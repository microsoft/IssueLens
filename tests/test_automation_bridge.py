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
URL = "https://issuelens.azurewebsites.net/api/issuelens/v1/jobs"
ENDPOINT = "https://account.services.ai.azure.com/test/protocols/invocations?api-version=2025-11-15-preview"
JOB = "7" * 64
COMMENT_URL = f"https://api.github.com/repos/{PILOT_REPOSITORY}/issues/comments/41"
ALLOWLIST_JSON = pathlib.Path(bridge.__file__).with_name("repository_allowlist.example.json").read_text(encoding="utf-8")


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

    def workflow_claims(self, token, audience, repository_id):
        allowlist = bridge.load_repository_allowlist(ALLOWLIST_JSON, PILOT_REPOSITORY_ID)
        return bridge.workflow_claims(token, audience, repository_id, allowlist)

    def load_ingress(self, allowlist=ALLOWLIST_JSON):
        self.enterContext(patch.dict(os.environ, {
            "ISSUELENS_BRIDGE_ROLE": "ingress", "ISSUELENS_BRIDGE_REPOSITORY_ID": str(PILOT_REPOSITORY_ID),
            "ISSUELENS_BRIDGE_OIDC_AUDIENCE": URL, "ISSUELENS_BRIDGE_QUEUE": "test-jobs",
            "ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST": allowlist,
        }))
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

    def test_java_pack_manual_dispatch_is_allowed_from_an_enrolled_workflow(self):
        accepted = self.workflow_claims(self.java_token(ref_protected="true"), URL, PILOT_REPOSITORY_ID)
        self.assertEqual(bridge.validate_request(self.java_payload(), accepted), "repo-104967329-issue-12")
        additional_workflow = self.workflow_claims(self.java_token(
            workflow_ref=bridge.workflow_ref(".github/workflows/another-triage.yaml", JAVA_REPOSITORY),
            ref_protected="true",
        ), URL, PILOT_REPOSITORY_ID)
        self.assertEqual(additional_workflow["kind"], "issue-loop")
        for overrides in (
            {"repository": PILOT_REPOSITORY}, {"repository_id": "104967330"}, {"repository_id": "not-an-id"},
            {"repository_visibility": "private"}, {"ref": "refs/tags/v1"}, {"ref_protected": "false"},
            {"runner_environment": "self-hosted"}, {"event_name": "issues"},
            {"event_name": "issue_comment"}, {"workflow_ref": bridge.workflow_ref(ISSUE_WORKFLOW)},
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

    def test_ingress_rejects_requests_if_configured_issuelens_id_disagrees_with_manifest(self):
        ingress, submit, status = self.load_ingress()
        with (
            patch.dict(os.environ, {"ISSUELENS_BRIDGE_REPOSITORY_ID": str(JAVA_REPOSITORY_ID)}),
            patch.object(ingress, "jobs") as jobs,
        ):
            self.assertEqual(submit(self.request()).status_code, 401)
            self.assertEqual(status(self.request(JOB)).status_code, 401)
            jobs.assert_not_called()

    def test_ingress_admits_a_new_configured_repository_without_changing_the_bundle(self):
        repository, repository_id = "example/project", 12345678
        workflow = ".github/workflows/triage.yml"
        ids = json.loads(ALLOWLIST_JSON)
        ids.append(repository_id)
        ingress, submit, _ = self.load_ingress(json.dumps(ids))
        token = self.token(
            repository=repository, repository_id=str(repository_id), event_name="workflow_dispatch",
            workflow_ref=bridge.workflow_ref(workflow, repository), ref_protected="true",
        )
        payload = self.java_payload(repository=repository)
        with patch.object(bridge.action, "github_read", return_value={
            "id": repository_id, "full_name": repository, "private": False,
            "visibility": "public", "default_branch": "main",
        }), patch.object(ingress, "jobs") as jobs, patch.object(ingress, "service_bus"):
            jobs.return_value.reserve.return_value = {"RowKey": JOB, "state": bridge.QUEUED}
            response = submit(self.request(token=token, payload=payload))
            self.assertEqual(response.status_code, 202, response.get_body())
            self.assertEqual(jobs.return_value.reserve.call_args.args[1], "repo-12345678-issue-12")

    def test_existing_team_memory_workflow_remains_enabled(self):
        claims = self.workflow_claims(self.token(
            workflow_ref=bridge.workflow_ref(MEMORY_WORKFLOW), event_name="workflow_dispatch",
            ref_protected="true",
        ), URL, PILOT_REPOSITORY_ID)
        metadata = {
            "repository": PILOT_REPOSITORY, "repository_id": PILOT_REPOSITORY_ID, "base_ref": "main",
            "event_name": "workflow_dispatch", "event_action": "workflow_dispatch",
            "actor_login": "maintainer", "triggering_actor": "maintainer",
            "workflow_ref": claims["workflow_ref"], "workflow_sha": claims["workflow_sha"],
            "run_id": 20, "run_attempt": 1, "coordinator_repository": PILOT_REPOSITORY,
            "coordinator_workflow_ref": claims["workflow_ref"], "coordinator_workflow_sha": claims["workflow_sha"],
            "coordinator_run_id": 20, "coordinator_run_attempt": 1, "required_wiki_repository": PILOT_REPOSITORY,
            "pull_number": 12, "merge_commit_sha": "a" * 40, "merged_at": "2026-11-24T00:00:00Z",
            "source_identity": f"{PILOT_REPOSITORY}#12:{'a' * 40}",
        }
        self.assertEqual(bridge.validate_request({"request_type": "team-memory", "metadata": metadata}, claims), "wiki")

    def test_java_pack_fallback_and_same_run_receipt_are_repository_bound(self):
        ingress, submit, status = self.load_ingress()
        token = self.java_token()
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
            {"payload": json.dumps(payload)}
        )
        self.assertEqual(json.loads(request.data), envelope["request"])

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


class BridgeAllowlistTests(unittest.TestCase):
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
        }
        for label, content in invalid.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                bridge.load_repository_allowlist(content, PILOT_REPOSITORY_ID)


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
            request = self.worker.prepare({"payload": json.dumps(payload)})
            github_request, = get.call_args.args
            self.assertEqual((github_request.full_url, github_request.get_method()), (COMMENT_URL, "GET"))
            self.assertFalse(github_request.has_header("Authorization"))
            self.assertEqual(github_request.get_header("Cache-control"), "no-cache")
        original_metadata = dict(payload["metadata"])
        original_metadata.pop("comment_sha256")
        self.assertEqual(json.loads(request.data), bridge.action.build_issue_loop_request(original_metadata))
        self.assertNotIn(b"test comment", request.data)

    def test_changed_or_unavailable_comment_never_reaches_foundry(self):
        job = {"state": bridge.RUNNING, "created": int(time.time()), "payload": json.dumps(issue_payload())}
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

    def test_conflicting_reservation_does_not_replace_the_active_job(self):
        conflict = TableTransactionError()
        conflict.status_code = 409
        store = bridge.JobStore(Mock())
        with patch.object(store, "get", side_effect=[None, None]):
            store.table.submit_transaction.side_effect = conflict
            with self.assertRaises(bridge.BusyError):
                store.reserve(JOB, "wiki", b"verified source metadata")

    def test_same_run_keeps_its_original_payload(self):
        store = bridge.JobStore(Mock())
        original = {"RowKey": JOB, "state": "queued", "payload": b"original"}
        store.table.get_entity.return_value = original
        self.assertIs(store.reserve(JOB, "wiki", b"replacement"), original)
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
            for role, expected in (("ingress", "status,submit"), ("worker", "process")):
                with self.subTest(role=role):
                    environment = {**os.environ, "ISSUELENS_BRIDGE_ROLE": role}
                    environment.pop("ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST", None)
                    environment.pop("ISSUELENS_BRIDGE_REPOSITORY_ID", None)
                    if role == "ingress":
                        environment["ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST"] = ALLOWLIST_JSON
                        environment["ISSUELENS_BRIDGE_REPOSITORY_ID"] = str(PILOT_REPOSITORY_ID)
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
            for value in (None, "", "{invalid"):
                environment = {**os.environ, "ISSUELENS_BRIDGE_ROLE": "ingress"}
                environment.pop("ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST", None)
                environment["ISSUELENS_BRIDGE_REPOSITORY_ID"] = str(PILOT_REPOSITORY_ID)
                if value is not None:
                    environment["ISSUELENS_BRIDGE_REPOSITORY_ALLOWLIST"] = value
                result = subprocess.run(
                    [sys.executable, "-c", "import function_app"], cwd=root,
                    env=environment,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
