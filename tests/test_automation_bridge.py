import hashlib
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
from cryptography.hazmat.primitives.asymmetric import rsa

from automation_bridge import bridge, bundle


PILOT_REPOSITORY = bridge.PILOT_REPOSITORY
URL = "https://issuelens.azurewebsites.net/api/issuelens/v1/jobs"
ENDPOINT = "https://account.services.ai.azure.com/test/protocols/invocations?api-version=2025-11-15-preview"
JOB = "7" * 64
COMMENT_URL = f"https://api.github.com/repos/{PILOT_REPOSITORY}/issues/comments/41"


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

    def test_only_the_protected_pilot_workflow_can_submit_or_read_its_job(self):
        now = int(time.time())
        claims = {
            "iss": bridge.GITHUB_ISSUER, "aud": URL, "iat": now, "exp": now + 60, "nbf": now,
            "repository": PILOT_REPOSITORY, "repository_id": "100", "repository_visibility": "public",
            "ref": bridge.BRANCH, "ref_protected": "true", "runner_environment": "github-hosted",
            "workflow_ref": bridge.workflow_ref(bridge.ISSUE_WORKFLOW), "workflow_sha": "1" * 40,
            "event_name": "issue_comment", "actor": "maintainer", "run_id": "20", "run_attempt": "1",
        }
        with patch.object(bridge.KEYS, "get_signing_key_from_jwt", return_value=Mock(key=self.key.public_key())):
            accepted = bridge.workflow_claims(jwt.encode(claims, self.key, algorithm="RS256"), URL, 100)
            self.assertEqual(bridge.validate_request(issue_payload(), accepted), "issue-12")
            for alteration in (
                {"repository_id": "101"}, {"ref_protected": "false"}, {"repository_visibility": "private"},
                {"event_name": "pull_request"}, {"workflow_ref": bridge.workflow_ref("another.yml")},
                {"aud": ENDPOINT}, {"exp": now - 1},
            ):
                with self.subTest(alteration=alteration), self.assertRaises((ValueError, jwt.PyJWTError)):
                    bridge.workflow_claims(jwt.encode(claims | alteration, self.key, algorithm="RS256"), URL, 100)
            with self.assertRaises(jwt.InvalidSignatureError):
                bridge.workflow_claims(
                    jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256"),
                    URL, 100,
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
                    result = subprocess.run(
                        [sys.executable, "-c", (
                            "from function_app import app; "
                            "print(','.join(sorted(x.get_function_name() for x in app.get_functions())))"
                        )],
                        cwd=root, env={**os.environ, "ISSUELENS_BRIDGE_ROLE": role},
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
