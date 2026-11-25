import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch
from zipfile import ZipFile

import jwt
from azure.data.tables import TableTransactionError
from azure.functions import HttpRequest
from cryptography.hazmat.primitives.asymmetric import rsa

from automation_bridge import bridge, bundle


PILOT_REPOSITORY = bridge.PILOT_REPOSITORY
PILOT_REPOSITORY_ID = 1303440932
BRANCH = "refs/heads/main"
ISSUE_WORKFLOW = ".github/workflows/issue-triage.yml"
MEMORY_WORKFLOW = ".github/workflows/team-memory-coordinator.yml"
JAVA_REPOSITORY = "microsoft/vscode-java-pack"
JAVA_REPOSITORY_ID = 104967329
JAVA_WORKFLOW = ".github/workflows/issueLens-run.yml"
OTHER_REPOSITORY = "example/Project-Two"
OTHER_REPOSITORY_ID = 24681012
OTHER_WORKFLOW = ".github/workflows/route-issues.yaml"
URL = "https://issuelens.azurewebsites.net/api/issuelens/v1/jobs"
ENDPOINT = "https://account.services.ai.azure.com/test/protocols/invocations?api-version=2025-11-15-preview"
JOB = "7" * 64
COMMENT_URL = f"https://api.github.com/repos/{PILOT_REPOSITORY}/issues/comments/41"
ALLOWLIST_JSON = pathlib.Path(bridge.__file__).with_name("repository_allowlist.example.json").read_text(encoding="utf-8")
EXTENDED_ALLOWLIST = json.dumps([*json.loads(ALLOWLIST_JSON), OTHER_REPOSITORY_ID])
JAVA_POLICY = {
    "repository": JAVA_REPOSITORY, "issue_workflow": JAVA_WORKFLOW,
    "issue_events": ["issues", "issue_comment", "workflow_dispatch"], "team_memory": True,
}
OTHER_POLICY = {
    "repository": OTHER_REPOSITORY, "issue_workflow": OTHER_WORKFLOW, "issue_events": ["issue_comment"],
}
POLICIES_JSON = json.dumps({JAVA_REPOSITORY_ID: JAVA_POLICY})


def issue_metadata(**overrides):
    return {
        "event_name": "issue_comment", "event_action": "created", "repository": PILOT_REPOSITORY,
        "issue_number": 12, "actor_login": "maintainer", "actor_type": "User",
        "issue_author_association": "NONE", "comment_id": 41, "comment_author_login": "maintainer",
        "comment_author_association": "MEMBER", "comment_added": True, "comment_edited": False,
        "manual_dispatch": False, "comment_sha256": hashlib.sha256(b"test comment").hexdigest(),
        **overrides,
    }


def issue_payload(**overrides):
    return {"request_type": "issue-loop", "metadata": issue_metadata(**overrides)}


def current_comment():
    return {
        "id": 41, "issue_url": f"https://api.github.com/repos/{PILOT_REPOSITORY}/issues/12",
        "body": "test comment", "created_at": "2026-11-24T00:00:00Z", "updated_at": "2026-11-24T00:00:00Z",
    }


def manual_memory_payload(claims):
    repository = claims["repository"]
    return {"request_type": "team-memory", "metadata": {
        "repository": repository, "repository_id": claims["repository_id"], "base_ref": "main",
        "event_name": "workflow_dispatch", "event_action": "workflow_dispatch",
        "actor_login": "maintainer", "triggering_actor": "maintainer",
        "workflow_ref": claims["workflow_ref"], "workflow_sha": claims["workflow_sha"],
        "run_id": 20, "run_attempt": 1, "coordinator_repository": repository,
        "coordinator_workflow_ref": claims["workflow_ref"], "coordinator_workflow_sha": claims["workflow_sha"],
        "coordinator_run_id": 20, "coordinator_run_attempt": 1, "required_wiki_repository": repository,
        "pull_number": 12, "merge_commit_sha": "a" * 40, "merged_at": "2026-11-24T00:00:00Z",
        "source_identity": f"{repository}#12:{'a' * 40}",
    }}


class BridgeIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        now = int(time.time())
        self.claims = {
            "iss": bridge.GITHUB_ISSUER, "aud": URL, "iat": now, "exp": now + 60, "nbf": now,
            "repository": PILOT_REPOSITORY, "repository_id": str(PILOT_REPOSITORY_ID), "repository_visibility": "public",
            "ref": BRANCH, "runner_environment": "github-hosted",
            "workflow_ref": bridge.workflow_ref(ISSUE_WORKFLOW), "workflow_sha": "1" * 40,
            "event_name": "issue_comment", "actor": "maintainer", "run_id": "20", "run_attempt": "1",
        }
        self.enterContext(patch.object(bridge.KEYS, "get_signing_key_from_jwt", return_value=Mock(key=self.key.public_key())))

    def token(self, **overrides):
        return jwt.encode(self.claims | overrides, self.key, algorithm="RS256")

    def workflow_claims(self, token, audience, repository_id, *, allowlist_json=ALLOWLIST_JSON,
                        policies_json=POLICIES_JSON):
        allowlist = bridge.load_repository_allowlist(allowlist_json, PILOT_REPOSITORY_ID)
        policies = bridge.load_repository_policies(policies_json, PILOT_REPOSITORY_ID, allowlist)
        return bridge.workflow_claims(token, audience, repository_id, allowlist, policies)

    def load_ingress(self, allowlist=ALLOWLIST_JSON, policies=POLICIES_JSON, pilot_id=PILOT_REPOSITORY_ID):
        self.enterContext(patch.dict(os.environ, {
            "ISSUELENS_BRIDGE_ROLE": "ingress", "ISSUELENS_BRIDGE_REPOSITORY_ID": str(pilot_id),
            "ISSUELENS_BRIDGE_OIDC_AUDIENCE": URL, "ISSUELENS_BRIDGE_QUEUE": "test-jobs",
            "ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST": allowlist,
        }))
        if policies is None:
            os.environ.pop("ISSUELENS_BRIDGE_REPOSITORY_POLICIES", None)
        else:
            os.environ["ISSUELENS_BRIDGE_REPOSITORY_POLICIES"] = policies
        path = pathlib.Path(bridge.__file__).with_name("function_app.py")
        spec = importlib.util.spec_from_file_location("test_bridge_ingress", path)
        assert spec is not None and spec.loader is not None
        ingress = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"bridge": bridge}):
            spec.loader.exec_module(ingress)
        handlers = {handler.get_function_name(): handler.get_user_function() for handler in ingress.app.get_functions()}
        return ingress, handlers["submit"], handlers["status"]

    def request(self, identifier=None, token=None, payload=None):
        headers = {"Authorization": f"Bearer {token or self.token()}"}
        if identifier is not None:
            return HttpRequest("GET", f"{URL}/{identifier}", route_params={"job_id": identifier}, headers=headers, body=b"")
        return HttpRequest("POST", URL, headers=headers, body=json.dumps(payload or issue_payload()).encode())

    def java_token(self, **overrides):
        return self.token(**({
            "repository": JAVA_REPOSITORY, "repository_id": str(JAVA_REPOSITORY_ID),
            "workflow_ref": bridge.workflow_ref(JAVA_WORKFLOW, JAVA_REPOSITORY),
            "event_name": "workflow_dispatch",
        } | overrides))

    def java_payload(self, **overrides):
        metadata = issue_metadata(
            repository=JAVA_REPOSITORY, event_name="workflow_dispatch", event_action="workflow_dispatch",
            comment_id=None, comment_author_login=None, comment_author_association=None,
            comment_added=False, comment_edited=False, manual_dispatch=True,
        )
        metadata.pop("comment_sha256")
        return {"request_type": "issue-loop", "metadata": metadata | overrides}

    def test_pilot_case_only_repository_rename_is_rejected(self):
        renamed = self.token(
            repository="microsoft/issuelens",
            workflow_ref=bridge.workflow_ref(".github/workflows/manual.yml", "microsoft/issuelens"),
            event_name="workflow_dispatch",
            ref_protected="true",
        )
        with self.assertRaisesRegex(ValueError, "Workflow is not in the IssueLens pilot scope"):
            self.workflow_claims(renamed, URL, PILOT_REPOSITORY_ID)

    def test_java_pack_admission_is_limited_to_its_reviewed_workflow(self):
        accepted = self.workflow_claims(self.java_token(ref_protected="true"), URL, PILOT_REPOSITORY_ID)
        self.assertEqual(bridge.validate_request(self.java_payload(), accepted), "repo-104967329-issue-12")
        for event in ("issues", "issue_comment", "workflow_dispatch"):
            self.assertEqual(self.workflow_claims(
                self.java_token(event_name=event, ref_protected="true"), URL, PILOT_REPOSITORY_ID,
            )["kind"], "issue-loop")
        for overrides in (
            {"repository": PILOT_REPOSITORY}, {"repository_id": "104967330"}, {"repository_id": "not-an-id"},
            {"repository_visibility": "private"}, {"ref": "refs/tags/v1"}, {"ref_protected": "false"},
            {"runner_environment": "self-hosted"}, {"event_name": "push"},
            {"repository": "microsoft/VSCode-java-pack"},
            {"ref": "refs/heads/other"}, {"workflow_ref": bridge.workflow_ref(ISSUE_WORKFLOW)},
            {"workflow_ref": bridge.workflow_ref(".github/workflows/another-triage.yaml", JAVA_REPOSITORY)},
            {"aud": ENDPOINT},
        ):
            with self.subTest(overrides=overrides), self.assertRaises((ValueError, jwt.PyJWTError)):
                self.workflow_claims(self.java_token(**overrides), URL, PILOT_REPOSITORY_ID)
        for payload in (
            self.java_payload(repository=PILOT_REPOSITORY),
            self.java_payload(event_name="issues", event_action="opened", manual_dispatch=False),
            self.java_payload(actor_login="other"),
            self.java_payload(input="untrusted task"),
            {"request_type": "team-memory", "metadata": self.java_payload()["metadata"]},
            {"request_type": "task", "metadata": self.java_payload()["metadata"]},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bridge.validate_request(payload, accepted)
        pilot = self.workflow_claims(self.token(ref_protected="true"), URL, PILOT_REPOSITORY_ID)
        self.assertEqual(bridge.validate_request(issue_payload(), pilot), "issue-12")
        self.assertNotEqual(bridge.job_id(pilot), bridge.job_id(accepted))
        self.assertEqual(bridge.job_id(accepted), bridge.job_id(accepted | {"run_attempt": 2}))

    def test_ingress_rejects_inconsistent_issuelens_id_or_unenrolled_policy_at_startup(self):
        for settings in (
            {"pilot_id": JAVA_REPOSITORY_ID},
            {"pilot_id": OTHER_REPOSITORY_ID},
            {"allowlist": json.dumps([PILOT_REPOSITORY_ID])},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.load_ingress(**settings)

    def test_id_only_enrollment_keeps_the_legacy_manual_contract(self):
        ref = "refs/heads/trunk"
        workflow = bridge.workflow_ref(".github/workflows/any-manual.yaml", JAVA_REPOSITORY, "trunk")
        for config in (None, json.dumps({OTHER_REPOSITORY_ID: OTHER_POLICY})):
            def authenticate(**overrides):
                token = self.java_token(**({"ref": ref, "workflow_ref": workflow} | overrides))
                return self.workflow_claims(
                    token, URL, PILOT_REPOSITORY_ID, allowlist_json=EXTENDED_ALLOWLIST, policies_json=config,
                )

            with self.subTest(config=config):
                claims = authenticate()
                self.assertEqual(bridge.validate_request(self.java_payload(), claims), "repo-104967329-issue-12")
                for event in ("issues", "issue_comment", "push"):
                    with self.assertRaises(ValueError):
                        authenticate(event_name=event)
                coordinator = authenticate(workflow_ref=bridge.workflow_ref(MEMORY_WORKFLOW, JAVA_REPOSITORY, "trunk"))
                self.assertEqual(coordinator["kind"], "issue-loop")
                with self.assertRaises(ValueError):
                    bridge.validate_request({"request_type": "team-memory", "metadata": {}}, coordinator)
        with self.assertRaises(ValueError):
            self.workflow_claims(self.java_token(), URL, PILOT_REPOSITORY_ID,
                                 allowlist_json=json.dumps([PILOT_REPOSITORY_ID]), policies_json=None)

    def test_detailed_policy_scopes_events_and_team_memory_per_repository(self):
        def authenticate(*, policy=OTHER_POLICY, **overrides):
            configured = json.dumps({JAVA_REPOSITORY_ID: JAVA_POLICY, OTHER_REPOSITORY_ID: policy})
            claims = {
                "repository": OTHER_REPOSITORY, "repository_id": str(OTHER_REPOSITORY_ID),
                "workflow_ref": bridge.workflow_ref(OTHER_WORKFLOW, OTHER_REPOSITORY),
            } | overrides
            return self.workflow_claims(self.token(**claims), URL, PILOT_REPOSITORY_ID,
                                        allowlist_json=EXTENDED_ALLOWLIST, policies_json=configured)

        claims = authenticate()
        payload = issue_payload(repository=OTHER_REPOSITORY)
        self.assertEqual(bridge.validate_request(payload, claims), f"repo-{OTHER_REPOSITORY_ID}-issue-12")
        java = self.workflow_claims(self.java_token(event_name="issue_comment"), URL, PILOT_REPOSITORY_ID)
        self.assertNotEqual(bridge.job_id(java), bridge.job_id(claims))
        coordinator = {"workflow_ref": bridge.workflow_ref(MEMORY_WORKFLOW, OTHER_REPOSITORY),
                       "event_name": "workflow_dispatch"}
        for mismatch in (
            {"event_name": "issues"}, {"event_name": "workflow_dispatch"}, coordinator,
            {"repository": JAVA_REPOSITORY}, {"repository_id": str(JAVA_REPOSITORY_ID)},
            {"repository": OTHER_REPOSITORY.lower()}, {"ref": "refs/heads/trunk"},
            {"workflow_ref": bridge.workflow_ref(OTHER_WORKFLOW.lower(), OTHER_REPOSITORY.lower())},
            {"workflow_ref": bridge.workflow_ref(JAVA_WORKFLOW, OTHER_REPOSITORY)},
        ):
            with self.subTest(mismatch=mismatch), self.assertRaises(ValueError):
                authenticate(**mismatch)
        memory = authenticate(policy=OTHER_POLICY | {"team_memory": True}, **coordinator)
        wiki_payload = manual_memory_payload(memory)
        self.assertEqual(bridge.validate_request(wiki_payload, memory), f"repo-{OTHER_REPOSITORY_ID}-wiki")
        self.assertNotEqual(bridge.job_id(memory), bridge.job_id(claims))
        wiki_payload["metadata"]["required_wiki_repository"] = JAVA_REPOSITORY
        with self.assertRaises(ValueError):
            bridge.validate_request(wiki_payload, memory)
        with self.assertRaises(ValueError):
            authenticate(policy=OTHER_POLICY | {"team_memory": True}, **(coordinator | {"event_name": "push"}))

    def test_detailed_policy_name_cannot_fall_back_to_an_id_only_enrollment(self):
        misleading_token = self.java_token(repository_id=str(OTHER_REPOSITORY_ID))
        with self.assertRaisesRegex(ValueError, "repository ID does not match"):
            self.workflow_claims(misleading_token, URL, PILOT_REPOSITORY_ID, allowlist_json=EXTENDED_ALLOWLIST)

    def test_java_pack_event_actions_follow_existing_issue_rules(self):
        for event, operation in (("issues", "opened"), ("issues", "reopened"),
                                 ("issue_comment", "created"), ("issue_comment", "edited")):
            claims = self.workflow_claims(self.java_token(event_name=event), URL, PILOT_REPOSITORY_ID)
            metadata = issue_metadata(repository=JAVA_REPOSITORY, event_name=event, event_action=operation,
                                      comment_added=operation == "created", comment_edited=operation == "edited")
            if event == "issues":
                metadata.update(comment_id=None, comment_author_login=None, comment_author_association=None)
            if operation != "created":
                metadata.pop("comment_sha256")
            with self.subTest(event=event, operation=operation):
                self.assertEqual(bridge.validate_request({"request_type": "issue-loop", "metadata": metadata}, claims),
                                 "repo-104967329-issue-12")
                with self.assertRaises(ValueError):
                    bridge.validate_request({"request_type": "issue-loop", "metadata": metadata | {"event_action": "deleted"}}, claims)
                if event == "issue_comment":
                    with self.assertRaises(ValueError):
                        bridge.validate_request({"request_type": "issue-loop", "metadata": metadata | {"actor_type": "Bot"}}, claims)

    def test_existing_team_memory_workflow_remains_enabled(self):
        claims = self.workflow_claims(self.token(
            workflow_ref=bridge.workflow_ref(MEMORY_WORKFLOW), event_name="workflow_dispatch",
            ref_protected="true",
        ), URL, PILOT_REPOSITORY_ID)
        self.assertEqual(bridge.validate_request(manual_memory_payload(claims), claims), "wiki")

    def test_packaged_java_coordinator_manual_and_push_contract(self):
        project = {"full_name": JAVA_REPOSITORY, "id": JAVA_REPOSITORY_ID,
                   "default_branch": "main", "visibility": "public"}
        coordinator_ref = bridge.workflow_ref(MEMORY_WORKFLOW, JAVA_REPOSITORY)
        before, after = "d" * 40, "e" * 40
        merged_at = "2026-11-24T00:00:00Z"
        pull = {"number": 12, "merged": True, "state": "closed", "merged_at": merged_at,
                "merge_commit_sha": after, "base": {"ref": "main", "repo": project}}
        source_run = {"id": 31, "run_attempt": 2, "event": "push",
                      "path": bridge.action.DISPATCH_WORKFLOW, "head_branch": "main", "head_sha": after,
                      "repository": project, "head_repository": project,
                      "actor": {"login": "maintainer"}, "triggering_actor": {"login": "maintainer"}}
        comparison = {"base_commit": {"sha": before}, "merge_base_commit": {"sha": before},
                      "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
                      "commits": [{"sha": after}]}
        node = {"number": 12, "state": "MERGED", "merged": True, "mergedAt": merged_at,
                "baseRefName": "main", "baseRepository": {"databaseId": JAVA_REPOSITORY_ID,
                                                           "nameWithOwner": JAVA_REPOSITORY},
                "mergeCommit": {"oid": after}}
        associations = {"data": {"repository": {
            "databaseId": JAVA_REPOSITORY_ID, "nameWithOwner": JAVA_REPOSITORY,
            "defaultBranchRef": {"name": "main", "target": {"oid": after}},
            "c0": {"oid": after, "associatedPullRequests": {
                "totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [node],
            }},
        }}}
        environment = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": BRANCH,
            "GITHUB_WORKFLOW_REF": coordinator_ref, "GITHUB_WORKFLOW_SHA": "1" * 40,
            "GITHUB_ACTOR": "maintainer", "GITHUB_TRIGGERING_ACTOR": "maintainer",
            "GITHUB_RUN_ID": "20", "GITHUB_RUN_ATTEMPT": "1", "SOURCE_REPOSITORY": JAVA_REPOSITORY,
            "SOURCE_REPOSITORIES": json.dumps({JAVA_REPOSITORY: JAVA_REPOSITORY_ID}),
            "SOURCE_GH_TOKEN": "fixture-token",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "bridge.zip"
            bundle.build(archive)
            with ZipFile(archive) as source:
                source.extractall(root)
            spec = importlib.util.spec_from_file_location("packaged_memory_bridge", root / "bridge.py")
            packaged = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(packaged)
            allowlist = packaged.load_repository_allowlist(ALLOWLIST_JSON, PILOT_REPOSITORY_ID)
            policies = packaged.load_repository_policies(POLICIES_JSON, PILOT_REPOSITORY_ID, allowlist)
            with patch.object(packaged.KEYS, "get_signing_key_from_jwt", return_value=Mock(key=self.key.public_key())):
                claims = packaged.workflow_claims(
                    self.java_token(workflow_ref=coordinator_ref, ref_protected="true"),
                    URL, PILOT_REPOSITORY_ID, allowlist, policies,
                )
                self.assertEqual(claims["kind"], "team-memory")
                for overrides in (
                    {"event_name": "push"}, {"workflow_ref": bridge.workflow_ref(MEMORY_WORKFLOW)},
                    {"ref": "refs/heads/other"}, {"runner_environment": "self-hosted"},
                    {"repository_id": str(PILOT_REPOSITORY_ID)},
                ):
                    with self.subTest(claims=overrides), self.assertRaises((ValueError, jwt.PyJWTError)):
                        packaged.workflow_claims(self.java_token(**({"workflow_ref": coordinator_ref} | overrides)),
                                                 URL, PILOT_REPOSITORY_ID, allowlist, policies)
            worker = packaged.FoundryWorker(ENDPOINT, Mock(get_token=Mock(return_value=Mock(token="token"))))
            for wrong_run in (
                source_run | {"repository": project | {"id": PILOT_REPOSITORY_ID}},
                source_run | {"head_repository": project | {"id": PILOT_REPOSITORY_ID}},
                source_run | {"id": 32},
            ):
                variables = {"SOURCE_RUN_ID": "31", "SOURCE_RUN_ATTEMPT": "2",
                             "PUSH_BEFORE": before, "PUSH_AFTER": after}
                with patch.dict(os.environ, environment | variables, clear=True), \
                        patch.object(packaged.action, "github_read", side_effect=[project, wrong_run]), \
                        self.assertRaises(ValueError):
                    packaged.action.prepare_team_memory(JAVA_REPOSITORY, {"repository": project})
            for mode in ("manual", "push"):
                variables = ({"DISPATCH_PR": "12"} if mode == "manual" else {
                    "SOURCE_RUN_ID": "31", "SOURCE_RUN_ATTEMPT": "2",
                    "PUSH_BEFORE": before, "PUSH_AFTER": after,
                })
                reads = ([project, pull] if mode == "manual" else [
                    project, source_run, {"name": "main", "commit": {"sha": after}}, comparison, associations,
                ])
                with self.subTest(mode=mode), patch.dict(os.environ, environment | variables, clear=True), \
                        patch.object(packaged.action, "github_read", side_effect=reads):
                    envelope = packaged.action.prepare_team_memory(JAVA_REPOSITORY, {"repository": project})
                metadata = envelope["metadata"]
                payload = {"request_type": "team-memory", "metadata": metadata}
                self.assertEqual(packaged.validate_request(payload, claims), "repo-104967329-wiki")
                job = {"repository": claims["repository"], "payload": json.dumps(payload)}
                self.assertEqual(json.loads(worker.prepare(job).data), envelope["request"])
                for key, wrong in (
                    ("repository", PILOT_REPOSITORY), ("repository_id", PILOT_REPOSITORY_ID),
                    ("coordinator_repository", PILOT_REPOSITORY), ("required_wiki_repository", PILOT_REPOSITORY),
                    ("coordinator_run_id", 21), ("coordinator_run_attempt", 2),
                    ("coordinator_workflow_sha", "2" * 40), ("source_identity", "wrong"),
                    ("workflow_ref", bridge.workflow_ref(MEMORY_WORKFLOW)),
                ):
                    invalid = copy.deepcopy(payload)
                    invalid["metadata"][key] = wrong
                    with self.subTest(mode=mode, key=key), self.assertRaises(ValueError):
                        packaged.validate_request(invalid, claims)
                if mode == "push":
                    invalid = copy.deepcopy(payload)
                    invalid["metadata"]["pull_requests"][0]["source_identity"] = f"{PILOT_REPOSITORY}#12:{after}"
                    with self.assertRaises(ValueError):
                        packaged.validate_request(invalid, claims)
                result = {"source_repository": JAVA_REPOSITORY, "status": "updated", "reason": "Verified",
                          "wiki_repository": JAVA_REPOSITORY, "wiki_sha": "f" * 40}
                if mode == "manual":
                    result.update(pull_number=12, merge_commit_sha=after)
                else:
                    result.update(push_before=before, push_after=after, results=[{
                        "pull_number": 12, "merge_commit_sha": after, "status": "updated", "reason": "Verified",
                    }])
                self.assertEqual(json.loads(packaged._memory_receipt(json.dumps(result), metadata))["status"], "updated")
                for field in ("source_repository", "wiki_repository"):
                    with self.subTest(mode=mode, result=field), self.assertRaises(ValueError):
                        packaged._memory_receipt(json.dumps(result | {field: PILOT_REPOSITORY}), metadata)
            legacy_environment = environment | {"DISPATCH_PR": "12"}
            del legacy_environment["SOURCE_REPOSITORY"]
            with patch.dict(os.environ, legacy_environment, clear=True), \
                    patch.object(packaged.action, "github_read", side_effect=[project, pull]):
                legacy = packaged.action.prepare_team_memory(JAVA_REPOSITORY, {"repository": project})
            with self.assertRaises(ValueError):
                packaged.validate_request({"request_type": "team-memory", "metadata": legacy["metadata"]}, claims)

    def test_java_memory_receipt_is_owned_by_the_coordinator_run(self):
        ingress, _, status = self.load_ingress()
        token = self.java_token(workflow_ref=bridge.workflow_ref(MEMORY_WORKFLOW, JAVA_REPOSITORY))
        claims = self.workflow_claims(token, URL, PILOT_REPOSITORY_ID)
        pilot = self.workflow_claims(self.token(
            workflow_ref=bridge.workflow_ref(MEMORY_WORKFLOW), event_name="workflow_dispatch",
        ), URL, PILOT_REPOSITORY_ID)
        identifier = bridge.job_id(claims)
        self.assertNotEqual(identifier, bridge.job_id(pilot))
        self.assertEqual(identifier, bridge.job_id(claims | {"run_attempt": 2}))
        with patch.object(ingress, "jobs") as jobs:
            jobs.return_value.get.return_value = {"RowKey": identifier, "state": bridge.COMPLETED}
            self.assertEqual(status(self.request(identifier, token=token)).status_code, 200)
            self.assertEqual(status(self.request(identifier)).status_code, 404)
            different_run = self.java_token(
                workflow_ref=bridge.workflow_ref(MEMORY_WORKFLOW, JAVA_REPOSITORY), run_id="21",
            )
            self.assertEqual(status(self.request(identifier, token=different_run)).status_code, 404)

    def test_detailed_policy_requires_current_canonical_repository_and_branch_protection(self):
        ingress, submit, status = self.load_ingress()
        token = self.java_token(ref_protected="true")
        claims = self.workflow_claims(token, URL, PILOT_REPOSITORY_ID)
        identifier = bridge.job_id(claims)
        repository_info = {
            "id": JAVA_REPOSITORY_ID, "full_name": JAVA_REPOSITORY, "private": False,
            "visibility": "public", "default_branch": "main",
        }
        with (
            patch.object(bridge.action, "github_read", side_effect=[repository_info, {"name": "main", "protected": True}]) as github,
            patch.object(ingress, "jobs") as jobs,
            patch.object(ingress, "service_bus"),
        ):
            jobs.return_value.reserve.return_value = {"RowKey": identifier, "state": bridge.QUEUED}
            jobs.return_value.get.return_value = {"RowKey": identifier, "state": bridge.COMPLETED}
            response = submit(self.request(token=token, payload=self.java_payload()))
            self.assertEqual(response.status_code, 202, response.get_body())
            self.assertEqual([call.args[0] for call in github.call_args_list], [
                "/repos/microsoft/vscode-java-pack",
                "/repos/microsoft/vscode-java-pack/branches/main",
            ])
            self.assertEqual(jobs.return_value.reserve.call_args.args[1], "repo-104967329-issue-12")
            self.assertEqual(jobs.return_value.reserve.call_args.kwargs, {"repository": JAVA_REPOSITORY})
            self.assertEqual(status(self.request(identifier, token=token)).status_code, 200)
            self.assertEqual(status(self.request(identifier)).status_code, 404)
        with (
            patch.object(bridge.action, "github_read", side_effect=[
                repository_info, {"name": "main", "protected": False},
            ]),
            patch.object(ingress, "jobs") as jobs,
        ):
            self.assertEqual(submit(self.request(token=token, payload=self.java_payload())).status_code, 401)
            jobs.assert_not_called()
        with (
            patch.object(bridge.action, "github_read",
                         return_value=repository_info | {"full_name": "microsoft/VSCode-java-pack"}),
            patch.object(ingress, "jobs") as jobs,
        ):
            self.assertEqual(submit(self.request(token=token, payload=self.java_payload())).status_code, 401)
            jobs.assert_not_called()

    def test_java_pack_action_preflight_produces_an_admissible_manual_job(self):
        project = {"full_name": JAVA_REPOSITORY, "id": JAVA_REPOSITORY_ID, "default_branch": "main"}
        env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": BRANCH,
            "GITHUB_WORKFLOW_REF": bridge.workflow_ref(JAVA_WORKFLOW, JAVA_REPOSITORY),
            "GITHUB_WORKFLOW_SHA": "1" * 40, "ISSUE_NUMBER": "12", "GITHUB_ACTOR": "maintainer",
            "TRANSPORT": "bridge",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(bridge.action, "github_read", side_effect=[project, {"number": 12}]),
        ):
            envelope = bridge.action.prepare_issue_loop(JAVA_REPOSITORY, {"repository": project})
        claims = self.workflow_claims(self.java_token(ref_protected="true"), URL, PILOT_REPOSITORY_ID)
        payload = {"request_type": "issue-loop", "metadata": envelope["metadata"]}
        self.assertEqual(bridge.validate_request(payload, claims), "repo-104967329-issue-12")
        request = bridge.FoundryWorker(ENDPOINT, Mock(get_token=Mock(return_value=Mock(token="token")))).prepare(
            {"repository": claims["repository"], "payload": json.dumps(payload)}
        )
        self.assertEqual(json.loads(request.data), envelope["request"])

    def test_packaged_action_and_bridge_handle_java_issue_and_comment_events(self):
        project = {"full_name": JAVA_REPOSITORY, "id": JAVA_REPOSITORY_ID, "default_branch": "main"}
        env = {
            "GITHUB_REF": BRANCH, "GITHUB_WORKFLOW_REF": bridge.workflow_ref(JAVA_WORKFLOW, JAVA_REPOSITORY),
            "GITHUB_WORKFLOW_SHA": "1" * 40, "GITHUB_ACTOR": "maintainer", "TRANSPORT": "bridge",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "bridge.zip"
            bundle.build(archive)
            with ZipFile(archive) as source:
                source.extractall(root)
            spec = importlib.util.spec_from_file_location("packaged_bridge", root / "bridge.py")
            packaged = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(packaged)
            allowlist = packaged.load_repository_allowlist(ALLOWLIST_JSON, PILOT_REPOSITORY_ID)
            policies = packaged.load_repository_policies(POLICIES_JSON, PILOT_REPOSITORY_ID, allowlist)
            worker = packaged.FoundryWorker(ENDPOINT, Mock(get_token=Mock(return_value=Mock(token="token"))))
            for event_name, event in (
                ("issues", {"action": "opened", "issue": {"number": 12},
                            "sender": {"login": "maintainer", "type": "User"}}),
                ("issue_comment", {"action": "created", "issue": {"number": 12},
                                   "comment": {"id": 41, "body": "test comment",
                                               "user": {"login": "maintainer", "type": "User"},
                                               "author_association": "MEMBER"},
                                   "sender": {"login": "maintainer", "type": "User"}}),
            ):
                event["repository"] = project
                with self.subTest(event=event_name), \
                        patch.dict(os.environ, env | {"GITHUB_EVENT_NAME": event_name}, clear=True), \
                        patch.object(packaged.KEYS, "get_signing_key_from_jwt", return_value=Mock(key=self.key.public_key())), \
                        patch.object(packaged.action, "github_read", return_value=project):
                    envelope = packaged.action.prepare_issue_loop(JAVA_REPOSITORY, event)
                    claims = packaged.workflow_claims(
                        self.java_token(event_name=event_name, ref_protected="true"),
                        URL, PILOT_REPOSITORY_ID, allowlist, policies,
                    )
                    payload = {"request_type": "issue-loop", "metadata": envelope["metadata"]}
                    self.assertEqual(packaged.validate_request(payload, claims), "repo-104967329-issue-12")
                    wrong = {"request_type": "issue-loop", "metadata": payload["metadata"] | {"repository": PILOT_REPOSITORY}}
                    with self.assertRaises(ValueError):
                        packaged.validate_request(wrong, claims)
                comment = current_comment() | {"issue_url": f"https://api.github.com/repos/{JAVA_REPOSITORY}/issues/12"}
                job = {"repository": claims["repository"], "payload": json.dumps(payload)}
                with patch.object(packaged.action, "github_read", return_value=comment) as read:
                    request = worker.prepare(job)
                    self.assertEqual(json.loads(request.data), envelope["request"])
                    if event_name == "issue_comment":
                        read.assert_called_once_with(f"/repos/{JAVA_REPOSITORY}/issues/comments/41", token="")
                    else:
                        read.assert_not_called()
                if event_name == "issue_comment":
                    with patch.object(packaged.action, "github_read", return_value=current_comment()):
                        with self.assertRaises(ValueError):
                            worker.prepare(job)

    def test_only_the_expected_workflow_identity_is_accepted(self):
        accepted = self.workflow_claims(self.token(ref_protected="true"), URL, PILOT_REPOSITORY_ID)
        self.assertEqual(bridge.validate_request(issue_payload(), accepted), "issue-12")
        for alteration in (
            {"repository_id": "101"}, {"ref": "refs/tags/v1"}, {"ref_protected": "false"},
            {"repository_visibility": "private"}, {"runner_environment": "self-hosted"},
            {"event_name": "pull_request"}, {"workflow_ref": bridge.workflow_ref("another.yml")},
            {"aud": ENDPOINT}, {"exp": self.claims["iat"] - 1},
        ):
            with self.subTest(alteration=alteration), self.assertRaises((ValueError, jwt.PyJWTError)):
                self.workflow_claims(self.token(**alteration), URL, PILOT_REPOSITORY_ID)
        with self.assertRaises(jwt.InvalidSignatureError):
            self.workflow_claims(
                jwt.encode(self.claims, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256"),
                URL, PILOT_REPOSITORY_ID,
            )
        self.assertEqual(bridge.job_id(accepted), bridge.job_id(accepted | {"run_attempt": 2}))
        self.assertNotEqual(bridge.job_id(accepted), bridge.job_id(accepted | {"run_id": 21}))
        for invalid in (
            issue_payload(repository="example/unrelated"), issue_payload(comment_added=False),
            issue_payload(body="caller-supplied context"), issue_payload(comment_author_login="other"),
            {"request_type": "team-memory", "metadata": issue_metadata()},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                bridge.validate_request(invalid, accepted)

    def test_token_without_protection_claim_can_submit_and_poll_a_protected_branch_job(self):
        ingress, submit, status = self.load_ingress()
        identifier = bridge.job_id({"kind": "issue-loop", "run_id": 20, "repository_id": PILOT_REPOSITORY_ID})
        repository_info = {
            "id": PILOT_REPOSITORY_ID, "full_name": PILOT_REPOSITORY, "private": False,
            "visibility": "public", "default_branch": "main",
        }
        with (
            patch.object(bridge.urllib.request, "build_opener") as opener,
            patch.object(ingress, "jobs") as jobs,
            patch.object(ingress, "service_bus") as bus,
        ):
            read = opener.return_value.open
            read.return_value.__enter__.return_value.read.side_effect = [
                json.dumps(repository_info).encode(), b'{"name":"main","protected":true}',
            ]
            jobs.return_value.reserve.return_value = {"RowKey": identifier, "state": bridge.QUEUED}
            jobs.return_value.get.return_value = {"RowKey": identifier, "state": bridge.COMPLETED}
            queued = submit(self.request())
            self.assertEqual(queued.status_code, 202, queued.get_body())
            self.assertEqual(json.loads(queued.get_body()), {"job_id": identifier, "state": bridge.QUEUED})
            jobs.return_value.reserve.assert_called_once()
            sender = bus.return_value.get_queue_sender.return_value.__enter__.return_value
            sender.send_messages.assert_called_once()

            completed = status(self.request(identifier))
            self.assertEqual(completed.status_code, 200, completed.get_body())
            self.assertEqual(json.loads(completed.get_body()), {"job_id": identifier, "state": bridge.COMPLETED})
            self.assertEqual(status(self.request("8" * 64)).status_code, 404)
            jobs.return_value.get.assert_called_once_with(identifier)
            self.assertEqual(read.call_count, 2)
            repository_request, branch_request = [call.args[0] for call in read.call_args_list]
            self.assertEqual(repository_request.full_url, f"https://api.github.com/repos/{PILOT_REPOSITORY}")
            self.assertEqual(branch_request.full_url, f"https://api.github.com/repos/{PILOT_REPOSITORY}/branches/main")
            self.assertEqual(branch_request.get_header("Cache-control"), "no-cache")
            self.assertFalse(branch_request.has_header("Authorization"))

    def test_missing_protection_claim_does_not_admit_jobs_without_confirmed_protection(self):
        ingress, submit, _ = self.load_ingress()
        repository_info = {
            "id": PILOT_REPOSITORY_ID, "full_name": PILOT_REPOSITORY, "private": False,
            "visibility": "public", "default_branch": "main",
        }
        for responses, expected in (
            ([repository_info, {"name": "main", "protected": False}], (401, {"error": "workflow_not_authorized"})),
            ([repository_info, {"name": "main"}], (503, {"error": "branch_protection_unavailable"})),
            ([repository_info, urllib.error.URLError("offline")], (503, {"error": "branch_protection_unavailable"})),
            ([repository_info | {"default_branch": "trunk"}], (401, {"error": "workflow_not_authorized"})),
            ([repository_info | {"id": 42}], (401, {"error": "workflow_not_authorized"})),
            ([repository_info | {"private": True}], (401, {"error": "workflow_not_authorized"})),
            ([repository_info | {"visibility": "internal"}], (401, {"error": "workflow_not_authorized"})),
            ([repository_info | {"full_name": "example/other"}], (401, {"error": "workflow_not_authorized"})),
        ):
            with (
                self.subTest(responses=responses),
                patch.object(bridge.action, "github_read", side_effect=responses) as github,
                patch.object(ingress, "jobs") as jobs,
                patch.object(ingress, "service_bus") as bus,
            ):
                result = submit(self.request())
                self.assertEqual((result.status_code, json.loads(result.get_body())), expected)
                self.assertEqual(github.call_count, len(responses))
                jobs.assert_not_called()
                bus.assert_not_called()


class BridgeConfigurationTests(unittest.TestCase):
    def test_invalid_id_allowlist_is_rejected_as_a_whole(self):
        original = ALLOWLIST_JSON
        ids = json.loads(original)
        invalid = {
            "duplicate ID": json.dumps(ids + [JAVA_REPOSITORY_ID]),
            "non-numeric ID": json.dumps(ids + [JAVA_REPOSITORY]),
            "boolean ID": json.dumps(ids + [True]),
            "empty": "",
            "oversize": " " * (32 * 1024 + 1),
            "object instead of list": json.dumps({"repositories": ids}),
            "missing IssueLens ID": json.dumps([JAVA_REPOSITORY_ID]),
            "non-positive ID": json.dumps(ids + [0]),
        }
        for label, content in invalid.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                bridge.load_repository_allowlist(content, PILOT_REPOSITORY_ID)

    def test_malformed_or_ambiguous_repository_policies_fail_closed(self):
        allowlist = bridge.load_repository_allowlist(EXTENDED_ALLOWLIST, PILOT_REPOSITORY_ID)
        other_id = str(OTHER_REPOSITORY_ID)
        with_duplicate_id = f"{{{json.dumps(other_id)}: {json.dumps(OTHER_POLICY)}, {json.dumps(other_id)}: null}}"
        invalid = {
            "empty string": "",
            "invalid JSON": "{",
            "oversize": " " * (32 * 1024 + 1),
            "array instead of object": "[]",
            "duplicate ID": with_duplicate_id,
            "non-canonical ID": json.dumps({"0" + other_id: OTHER_POLICY}),
            "unenrolled ID": json.dumps({42: OTHER_POLICY}),
            "IssueLens ID": json.dumps({PILOT_REPOSITORY_ID: OTHER_POLICY}),
            "IssueLens name": json.dumps({other_id: OTHER_POLICY | {"repository": PILOT_REPOSITORY.lower()}}),
            "duplicate name": json.dumps({
                JAVA_REPOSITORY_ID: JAVA_POLICY, other_id: OTHER_POLICY | {"repository": JAVA_REPOSITORY.upper()},
            }),
        }
        for field_override in (
            {"repo": OTHER_REPOSITORY}, {"repository": "https://github.com/example/repo"},
            {"issue_workflow": ".github/workflows/subdir/issue.yml"},
            {"issue_workflow": MEMORY_WORKFLOW}, {"issue_workflow": bridge.action.DISPATCH_WORKFLOW},
            {"issue_events": ["pull_request"]}, {"issue_events": ["issue_comment", "issue_comment"]},
            {"issue_events": []}, {"issue_events": [None]}, {"team_memory": "true"},
        ):
            invalid[str(field_override)] = json.dumps({other_id: OTHER_POLICY | field_override})
        for label, content in invalid.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                bridge.load_repository_policies(content, PILOT_REPOSITORY_ID, allowlist)


class BridgeActionTests(unittest.TestCase):
    def test_workflow_token_uses_the_bridge_audience_and_a_github_origin(self):
        endpoint = "https://tokenghub.actions.githubusercontent.com/_apis/idtoken?api-version=2.0&audience=previous"
        env = {"ACTIONS_ID_TOKEN_REQUEST_URL": endpoint, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-secret"}
        with patch.dict(os.environ, env, clear=True), patch.object(bridge.action, "bridge_json") as request:
            request.return_value = {"value": "header.payload.signature"}
            self.assertEqual(bridge.action.workflow_token(URL), "header.payload.signature")
            request_url, credential = request.call_args.args
            self.assertEqual(credential, "runner-secret")
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request_url).query),
                             {"api-version": ["2.0"], "audience": [URL]})
            os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] = "https://tokenghub.actions.githubusercontent.com.example.test/"
            with self.assertRaisesRegex(ValueError, "OIDC endpoint unavailable"):
                bridge.action.workflow_token(URL)
            request.assert_called_once()

    def test_issue_preflight_uses_a_digest_without_relaying_the_comment(self):
        event = {
            "action": "created", "issue": {"number": 12, "body": "private event content"},
            "comment": {
                "id": 41, "user": {"login": "maintainer", "type": "User"}, "body": "test comment",
                "author_association": "MEMBER",
            },
            "sender": {"login": "maintainer", "type": "User"},
        }
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "issue_comment", "TRANSPORT": "bridge"}, clear=True),
            patch.object(bridge.action, "validate_workflow"),
        ):
            envelope = bridge.action.prepare_issue_loop(PILOT_REPOSITORY, event)
        self.assertEqual(envelope["metadata"]["comment_sha256"], issue_metadata()["comment_sha256"])
        self.assertNotIn("comment_sha256", envelope["request"]["input"])
        self.assertNotIn("test comment", json.dumps(envelope))
        self.assertNotIn("private event content", json.dumps(envelope))

    def test_action_submits_once_and_waits_for_terminal_result(self):
        env = {
            "TRANSPORT": "bridge", "BRIDGE_URL": URL, "GH_TOKEN": "unforwarded-github-token",
        }
        envelope = {**issue_payload(), "request": {"input": "unforwarded legacy prompt"}}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(bridge.action, "workflow_token", return_value="function-audience-jwt"),
            patch.object(bridge.action, "bridge_json", side_effect=[
                {"job_id": JOB, "state": "queued"}, {"job_id": JOB, "state": "running"},
                {"job_id": JOB, "state": "completed"},
            ]) as request,
            patch.object(bridge.action.time, "sleep"),
            patch.object(bridge.action, "display_log"),
            patch.object(bridge.action.subprocess, "check_output") as az,
        ):
            self.assertIn("Inspect GitHub", bridge.action.submit_bridge(envelope))
        self.assertEqual(
            [call.args for call in request.call_args_list],
            [(URL, "function-audience-jwt", issue_payload()),
             (f"{URL}/{JOB}", "function-audience-jwt"), (f"{URL}/{JOB}", "function-audience-jwt")],
        )
        az.assert_not_called()

    def test_unknown_submission_is_not_replayed(self):
        with (
            patch.dict(os.environ, {"BRIDGE_URL": URL}),
            patch.object(bridge.action, "workflow_token", return_value="workflow-token"),
            patch.object(bridge.action, "bridge_json", side_effect=[
                {"job_id": JOB, "state": "unknown"},
            ]) as request,
            patch.object(bridge.action, "display_log"),
        ):
            with self.assertRaisesRegex(ValueError, "outcome is unknown"):
                bridge.action.submit_bridge(issue_payload())
        request.assert_called_once()


class BridgeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.credential = Mock()
        self.credential.get_token.return_value.token = "managed-identity-token"
        self.worker = bridge.FoundryWorker(ENDPOINT, self.credential)

    def test_worker_uses_the_existing_foundry_api_for_issue_and_wiki_jobs(self):
        issue = issue_metadata(event_name="issues", event_action="opened", comment_id=None, comment_added=False,
                               comment_author_login=None, comment_author_association=None)
        issue.pop("comment_sha256")
        memory = {
            "repository": PILOT_REPOSITORY, "event_name": "workflow_dispatch",
            "pull_number": 12, "merge_commit_sha": "a" * 40, "required_wiki_repository": PILOT_REPOSITORY,
        }
        with patch.object(bridge.action, "github_read") as github:
            for kind, metadata, expected in (
                ("issue-loop", issue, bridge.action.build_issue_loop_request(issue)),
                ("team-memory", memory, bridge.action.build_team_memory_request(memory)),
            ):
                with self.subTest(kind=kind):
                    payload = {"request_type": kind, "metadata": metadata}
                    request = self.worker.prepare({"payload": json.dumps(payload)})
                    self.assertEqual((request.full_url, request.method), (ENDPOINT, "POST"))
                    self.assertEqual(request.get_header("Authorization"), "Bearer managed-identity-token")
                    self.assertEqual(request.get_header("Accept"), "text/event-stream")
                    self.assertEqual(json.loads(request.data), expected)
        github.assert_not_called()
        self.credential.get_token.assert_called_with("https://ai.azure.com/.default")

    def test_worker_verifies_created_comment_without_forwarding_body_or_digest(self):
        payload = issue_payload()
        with patch.dict(os.environ, {"GH_TOKEN": "do-not-forward"}), \
                patch.object(bridge.urllib.request, "build_opener") as opener:
            get = opener.return_value.open
            get.return_value.__enter__.return_value.read.return_value = json.dumps(current_comment()).encode()
            request = self.worker.prepare({"target": "issue-12", "payload": json.dumps(payload)})
            github_request, = get.call_args.args
            self.assertEqual((github_request.full_url, github_request.get_method()), (COMMENT_URL, "GET"))
            self.assertFalse(github_request.has_header("Authorization"))
            self.assertEqual(github_request.get_header("Cache-control"), "no-cache")
        original_metadata = dict(payload["metadata"])
        original_metadata.pop("comment_sha256")
        self.assertEqual(json.loads(request.data), bridge.action.build_issue_loop_request(original_metadata))
        self.assertNotIn(b"test comment", request.data)

    def test_changed_or_unavailable_comment_never_reaches_foundry(self):
        job = {"state": bridge.RUNNING, "created": int(time.time()), "repository": PILOT_REPOSITORY,
               "payload": json.dumps(issue_payload())}
        for comment in (
            {**current_comment(), "body": "Edited within the same second"},
            {**current_comment(), "updated_at": "2026-11-24T00:00:01Z"},
            {**current_comment(), "issue_url": current_comment()["issue_url"] + "3"},
            urllib.error.URLError("GitHub unavailable"),
        ):
            with self.subTest(comment=comment), \
                    patch.object(bridge.action, "github_read", side_effect=[comment]), \
                    patch.object(bridge.urllib.request, "build_opener") as opener:
                store = Mock(spec=bridge.JobStore)
                store.get.side_effect = [job | {"state": bridge.QUEUED}, job]
                self.assertEqual(self.worker.run(store, JOB), bridge.FAILED)
                store.settle.assert_called_once_with(job, bridge.FAILED)
                opener.assert_not_called()
        self.credential.get_token.assert_not_called()

    def test_worker_uses_durably_admitted_repository_not_the_queued_payload_for_comments(self):
        store = bridge.JobStore(Mock())
        target = f"repo-{OTHER_REPOSITORY_ID}-issue-12"
        with patch.object(store, "get", return_value=None):
            job = store.reserve(JOB, target, json.dumps(issue_payload(repository=OTHER_REPOSITORY)).encode(),
                                repository=OTHER_REPOSITORY)
        operations, = store.table.submit_transaction.call_args.args
        self.assertEqual(operations[1][1]["repository"], OTHER_REPOSITORY)
        current = current_comment() | {"issue_url": f"https://api.github.com/repos/{OTHER_REPOSITORY}/issues/12"}
        with patch.object(bridge.action, "github_read", return_value=current) as read:
            self.worker.prepare(job)
        read.assert_called_once_with(f"/repos/{OTHER_REPOSITORY}/issues/comments/41", token="")
        tampered = job | {"payload": json.dumps(issue_payload())}
        unbound = {key: value for key, value in job.items() if key != "repository"}
        self.credential.reset_mock()
        with patch.object(bridge.action, "github_read") as read:
            for rejected in (tampered, unbound, unbound | {"payload": json.dumps(issue_payload())}):
                with self.subTest(job=rejected), self.assertRaises(ValueError):
                    self.worker.prepare(rejected)
        read.assert_not_called()
        self.credential.get_token.assert_not_called()

    def test_conflicting_reservation_does_not_replace_the_active_job(self):
        conflict = TableTransactionError()
        conflict.status_code = 409
        store = bridge.JobStore(Mock())
        with patch.object(store, "get", side_effect=[None, None]):
            store.table.submit_transaction.side_effect = conflict
            with self.assertRaises(bridge.BusyError):
                store.reserve(JOB, "wiki", b"verified source metadata", repository=PILOT_REPOSITORY)

    def test_same_run_keeps_its_original_payload(self):
        store = bridge.JobStore(Mock())
        original = {"RowKey": JOB, "state": "queued", "payload": b"original"}
        store.table.get_entity.return_value = original
        self.assertIs(store.reserve(JOB, "wiki", b"replacement", repository=PILOT_REPOSITORY), original)
        store.table.submit_transaction.assert_not_called()

    def test_settlement_does_not_release_a_different_jobs_reservation(self):
        class Entity(dict):
            metadata = {"etag": "current"}

        store = bridge.JobStore(Mock())
        job = Entity(PartitionKey=bridge.PARTITION, RowKey=JOB, target="wiki", state="running", payload=b"metadata")
        lock = Entity(PartitionKey=bridge.PARTITION, RowKey="lock-wiki", job_id="8" * 64)
        store.table.get_entity.return_value = lock
        with self.assertRaisesRegex(ValueError, "does not own"):
            store.settle(job, bridge.COMPLETED)
        store.table.submit_transaction.assert_not_called()
        lock["job_id"] = JOB
        store.settle(job, bridge.COMPLETED)
        operations, = store.table.submit_transaction.call_args.args
        self.assertEqual([entry[0] for entry in operations], ["update", "delete"])
        self.assertEqual(operations[0][1]["state"], "completed")
        self.assertEqual(operations[1][2], {"etag": "current", "match_condition": bridge.MatchConditions.IfNotModified})

    def test_duplicate_queue_delivery_cannot_restart_a_running_or_unknown_job(self):
        store = Mock(spec=bridge.JobStore)
        for state in ("running", "unknown", "completed"):
            with self.subTest(state=state):
                store.get.return_value = {"RowKey": JOB, "state": state}
                self.assertIsNone(self.worker.run(store, JOB))
        store.claim.assert_not_called()

    def test_interrupted_foundry_response_holds_the_reservation(self):
        job = Mock(metadata={"etag": "1"})
        claimed = {
            "PartitionKey": bridge.PARTITION, "RowKey": JOB, "target": "wiki", "state": "running",
            "created": int(time.time()), "payload": b"metadata",
        }

        class Entity(dict):
            metadata = job.metadata

        table = Mock()
        table.get_entity.side_effect = [Entity(claimed | {"state": "queued"}), Entity(claimed)]
        store = bridge.JobStore(table)
        with (
            patch.object(self.worker, "prepare", return_value=Mock()),
            patch.object(bridge.urllib.request, "build_opener") as opener,
        ):
            opener.return_value.open.side_effect = urllib.error.URLError("connection lost after send")
            self.assertEqual(self.worker.run(store, JOB), "unknown")
        opener.return_value.open.assert_called_once()
        operations, = table.submit_transaction.call_args.args
        self.assertEqual([entry[0] for entry in operations], ["update"])
        self.assertEqual(operations[0][1]["state"], "unknown")
        self.assertNotIn("payload", operations[0][1])

    def test_partial_wiki_receipt_keeps_every_pr_without_persisting_agent_prose(self):
        repository = PILOT_REPOSITORY
        first, second = "a" * 40, "b" * 40
        metadata = {
            "event_name": "push", "repository": repository, "required_wiki_repository": repository,
            "push_before": "1" * 40, "push_after": "2" * 40,
            "pull_requests": [{"pull_number": 12, "merge_commit_sha": first},
                              {"pull_number": 13, "merge_commit_sha": second}],
        }
        parsed = {
            "source_repository": repository, "status": "partial", "reason": "unforwarded model prose",
            "wiki_repository": repository, "wiki_sha": "f" * 40,
            "push_before": metadata["push_before"], "push_after": metadata["push_after"],
            "results": [
                {"pull_number": 12, "merge_commit_sha": first, "status": "updated", "reason": "publication reason"},
                {"pull_number": 13, "merge_commit_sha": second, "status": "needs-review", "reason": "unfinished evidence"},
            ],
        }
        durable = bridge._memory_receipt(json.dumps(parsed), metadata)
        value = json.loads(durable)
        bridge.action.validate_team_memory_result(value, metadata)
        self.assertEqual(value["status"], "partial")
        self.assertEqual([(pr["pull_number"], pr["status"]) for pr in value["results"]],
                         [(12, "updated"), (13, "needs-review")])
        for prose in (b"unforwarded model prose", b"unfinished evidence"):
            self.assertNotIn(prose, durable)
        with self.assertRaises(ValueError):
            bridge._memory_receipt(json.dumps(parsed | {"wiki_repository": "example/wiki"}), metadata)

    def test_bundle_indexes_only_the_expected_trigger_for_each_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "bridge.zip"
            bundle.build(archive)
            with ZipFile(archive) as source:
                self.assertEqual(set(source.namelist()), {
                    "function_app.py", "bridge.py", "host.json", "requirements.txt",
                    "issuelens_action.py", "stream_output.py",
                })
                source.extractall(root)
            for role, expected, policies in (
                ("ingress", "status,submit", None), ("ingress", "status,submit", POLICIES_JSON),
                ("worker", "process", None),
            ):
                with self.subTest(role=role, policies=policies):
                    environment = {**os.environ, "ISSUELENS_BRIDGE_ROLE": role}
                    environment.pop("ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST", None)
                    environment.pop("ISSUELENS_BRIDGE_REPOSITORY_ID", None)
                    environment.pop("ISSUELENS_BRIDGE_REPOSITORY_POLICIES", None)
                    if role == "ingress":
                        environment["ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST"] = ALLOWLIST_JSON
                        environment["ISSUELENS_BRIDGE_REPOSITORY_ID"] = str(PILOT_REPOSITORY_ID)
                    if policies is not None:
                        environment["ISSUELENS_BRIDGE_REPOSITORY_POLICIES"] = policies
                    result = subprocess.run(
                        [sys.executable, "-c", (
                            "from inspect import signature\n"
                            "from function_app import app\n"
                            "functions = app.get_functions()\n"
                            "for function in functions:\n"
                            "    trigger = function.get_trigger().get_dict_repr()['name']\n"
                            "    assert trigger in signature(function.get_user_function()).parameters, "
                            "(function.get_function_name(), trigger)\n"
                            "print(','.join(sorted(x.get_function_name() for x in functions)))"
                        )],
                        cwd=root, env=environment,
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected)
            allowlist_setting = "ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST"
            policy_setting = "ISSUELENS_BRIDGE_REPOSITORY_POLICIES"
            for setting, value in ((allowlist_setting, None), (allowlist_setting, ""), (policy_setting, "{invalid")):
                environment = {**os.environ, "ISSUELENS_BRIDGE_ROLE": "ingress"}
                environment[allowlist_setting] = ALLOWLIST_JSON
                environment.pop(policy_setting, None)
                environment["ISSUELENS_BRIDGE_REPOSITORY_ID"] = str(PILOT_REPOSITORY_ID)
                if value is None:
                    del environment[setting]
                else:
                    environment[setting] = value
                result = subprocess.run(
                    [sys.executable, "-c", "import function_app"], cwd=root,
                    env=environment,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
