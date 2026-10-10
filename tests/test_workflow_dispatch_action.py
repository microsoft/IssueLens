import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

import yaml


ACTION = Path(__file__).parents[1] / ".github" / "actions" / "queue-team-memory"


class Response(io.BytesIO):
    def __init__(self, value=b"", status=200):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = status


class WorkflowDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.bundle = self.directory / "standalone-action"
        shutil.copytree(ACTION, self.bundle, ignore=shutil.ignore_patterns("__pycache__"))
        spec = importlib.util.spec_from_file_location("standalone_dispatch", self.bundle / "dispatch.py")
        self.client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.client)
        self.environment = {
            "COORDINATOR_REPOSITORY": "example/central", "COORDINATOR_WORKFLOW": "queue.yml",
            "COORDINATOR_REF": "main", "DISPATCH_TOKEN": "fixture-dispatch-credential",
            "WORKFLOW_INPUTS": '{"job":"fixture","request_id":"123"}',
            "GH_TOKEN": "fixture-source-credential",
            "GITHUB_REPOSITORY": "example/source", "GITHUB_REF": "refs/heads/develop",
        }
        self.project = {"id": 200, "full_name": "example/central"}
        self.workflow = {"id": 300, "path": ".github/workflows/queue.yml", "state": "active"}
        self.branch = {"name": "main", "commit": {"sha": "f" * 40}}
        self.output = io.StringIO()

    def responses(self, *values):
        return [Response(value) for value in values]

    def execute(self, responses):
        self.opener = Mock()
        self.opener.open.side_effect = responses
        with patch.object(self.client.os, "environ", self.environment), contextlib.redirect_stdout(self.output), \
                patch.object(self.client.urllib.request, "build_opener", return_value=self.opener) as builder:
            self.builder = builder
            self.client.run()

    def success(self):
        return self.responses(self.project, self.workflow, self.branch) + [Response(status=204)]

    def test_copied_action_is_standalone_and_has_only_dispatch_wiring(self):
        metadata = yaml.load((self.bundle / "action.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(metadata["inputs"]), {
            "dispatch-token", "coordinator-repository", "coordinator-workflow", "coordinator-ref", "workflow-inputs",
        })
        self.assertEqual(metadata["inputs"]["coordinator-repository"]["default"], "${{ github.repository }}")
        self.assertEqual(metadata["inputs"]["coordinator-ref"]["default"], "${{ github.ref_name }}")
        self.assertEqual(metadata["inputs"]["dispatch-token"]["default"], "${{ github.token }}")
        self.assertEqual(metadata["inputs"]["workflow-inputs"]["default"], "{}")
        self.assertEqual(metadata["inputs"]["coordinator-workflow"]["required"], "true")
        step, = metadata["runs"]["steps"]
        self.assertEqual(step["run"], 'python3 -I "$GITHUB_ACTION_PATH/dispatch.py"')
        self.assertNotIn("${{", step["run"])
        self.assertNotIn("outputs", metadata)
        for forbidden in ("../issuelens", "source-token", "SOURCE_", "GITHUB_EVENT", "RUNNER_TEMP",
                          "upload-artifact", "download-artifact", "azure/login", "wiki", "prepare_push"):
            self.assertNotIn(forbidden, (self.bundle / "dispatch.py").read_text() + json.dumps(metadata))
        self.assertFalse((self.directory / "issuelens").exists())
        self.execute(self.success())
        self.assertEqual(self.opener.open.call_count, 4)

    def test_single_authenticated_post_preserves_arbitrary_caller_inputs_and_branch_independence(self):
        self.execute(self.success())
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual([request.full_url for request in requests], [
            "https://api.github.com/repos/example/central",
            "https://api.github.com/repos/example/central/actions/workflows/queue.yml",
            "https://api.github.com/repos/example/central/branches/main",
            "https://api.github.com/repos/example/central/actions/workflows/queue.yml/dispatches",
        ])
        self.assertEqual([request.get_method() for request in requests], ["GET"] * 3 + ["POST"])
        self.assertEqual(json.loads(requests[-1].data), {"ref": "main", "inputs": {"job": "fixture", "request_id": "123"}})
        self.assertTrue(all(request.get_header("Authorization") == "Bearer " + self.environment["DISPATCH_TOKEN"]
                            for request in requests))
        self.assertTrue(all(call.kwargs["timeout"] == 30 for call in self.opener.open.call_args_list))
        self.assertIsNone(self.builder.call_args.args[0].redirect_request(None, None, None, None, None, None))
        for credential in (self.environment["GH_TOKEN"], self.environment["DISPATCH_TOKEN"]):
            self.assertNotIn(credential.encode(), requests[-1].data)
            self.assertNotIn(credential, self.output.getvalue())
        self.assertIn("job completion are not confirmed", self.output.getvalue())

    def test_same_repo_pilot_and_cross_repo_job_payloads_are_caller_owned(self):
        cases = [
            ("example/central", {"source_run_id": "123456", "source_run_attempt": "2",
                                 "push_before": "a" * 40, "push_after": "b" * 40}),
            ("example/source", {"source_repository": "example/source", "source_run_id": "123456",
                                "source_run_attempt": "2", "push_before": "a" * 40, "push_after": "b" * 40}),
            ("unrelated/source", {"pull_request_number": "27"}),
        ]
        for source, inputs in cases:
            with self.subTest(source=source):
                self.environment["GITHUB_REPOSITORY"] = source
                self.environment["WORKFLOW_INPUTS"] = json.dumps(inputs)
                self.execute(self.success())
                self.assertEqual(json.loads(self.opener.open.call_args.args[0].data),
                                 {"ref": "main", "inputs": inputs})

    def test_branch_path_is_url_encoded(self):
        self.environment["COORDINATOR_REF"] = "release/1.0"
        self.branch["name"] = "release/1.0"
        self.execute(self.success())
        self.assertTrue(self.opener.open.call_args_list[2].args[0].full_url.endswith("/branches/release%2F1.0"))

    def test_invalid_targets_tokens_and_payloads_fail_before_network(self):
        original = self.environment.copy()
        invalid = {
            "COORDINATOR_REPOSITORY": ("", "../repo", "https://github.com/a/b", "a/b/c", "a/b\n", "a/b?x"),
            "COORDINATOR_WORKFLOW": ("", "../queue.yml", ".github/workflows/queue.yml", "queue.json", "queue.yml\n"),
            "COORDINATOR_REF": ("", "refs/heads/main", "main..next", "main.lock", "main\n", "main@{0}", "main;echo",
                                "/main", "main//next", "main/", "x" * 256),
            "DISPATCH_TOKEN": ("", " ", "token\nnext"),
            "WORKFLOW_INPUTS": ("", "[]", "null", "{", '{"x":true}', '{"x":1}', '{"x":{}}', '{"x":[]}',
                                '{"x":"a","x":"b"}', '{"x\\ny":"a"}', "x" * (64 * 1024 + 1),
                                json.dumps({f"key{i}": "v" for i in range(11)}),
                                json.dumps({"credential": self.environment["DISPATCH_TOKEN"]})),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value[:40]):
                    self.environment = {**original, name: value}
                    with self.assertRaises(SystemExit) as raised:
                        self.execute([])
                    self.opener.open.assert_not_called()
                    self.assertNotIn(original["DISPATCH_TOKEN"], str(raised.exception))

    def test_exact_payload_budget_and_input_count(self):
        self.environment["WORKFLOW_INPUTS"] = json.dumps({f"key{i}": "value" for i in range(10)})
        self.execute(self.success())
        reference = self.environment["COORDINATOR_REF"]
        overhead = len(json.dumps({"ref": reference, "inputs": {"x": ""}}).encode())
        self.environment["WORKFLOW_INPUTS"] = json.dumps({"x": "a" * (self.client.MAX_BYTES - overhead)})
        self.execute(self.success())
        self.assertEqual(len(self.opener.open.call_args.args[0].data), self.client.MAX_BYTES)
        self.environment["WORKFLOW_INPUTS"] = json.dumps({"x": "a" * (self.client.MAX_BYTES - overhead + 1)})
        with self.assertRaisesRegex(SystemExit, "payload exceeds"):
            self.execute([])
        self.opener.open.assert_not_called()

    def test_target_authentication_identity_and_bounded_reads_fail_before_post(self):
        cases = [
            ([{**self.project, "full_name": "other/target"}], 1),
            ([{**self.project, "id": True}], 1),
            ([self.project, {**self.workflow, "path": ".github/workflows/other.yml"}], 2),
            ([self.project, {**self.workflow, "state": "disabled_manually"}], 2),
            ([self.project, self.workflow, {**self.branch, "name": "develop"}], 3),
            ([self.project, self.workflow, {"name": "main", "commit": {"sha": "0" * 40}}], 3),
        ]
        for values, count in cases:
            with self.subTest(values=values):
                with self.assertRaises(SystemExit):
                    self.execute(self.responses(*values))
                self.assertEqual(self.opener.open.call_count, count)
                self.assertTrue(all(call.args[0].get_method() == "GET" for call in self.opener.open.call_args_list))
        for response in (Response(b"x" * (self.client.MAX_BYTES + 1)), Response(b"PRIVATE INVALID JSON"),
                         Response(b"[]"), Response(status=302),
                         ValueError("PRIVATE TRANSPORT DETAIL"),
                         urllib.error.HTTPError("https://api.github.com", 403, "PRIVATE DETAIL", {}, None)):
            with self.subTest(response=response):
                with self.assertRaises(SystemExit) as raised:
                    self.execute([response])
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.opener.open.assert_called_once()

    def test_unknown_or_unacknowledged_post_is_not_retried_or_reported_as_job_success(self):
        for response in (Response(status=202), Response(status=302), OSError("PRIVATE DETAIL"),
                         ValueError("PRIVATE TRANSPORT DETAIL"),
                         urllib.error.HTTPError("https://api.github.com", 500, "PRIVATE DETAIL", {}, None)):
            with self.subTest(response=response):
                with self.assertRaises(SystemExit) as raised:
                    self.execute(self.responses(self.project, self.workflow, self.branch) + [response])
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.assertEqual(self.opener.open.call_count, 4)
                self.assertEqual(sum(call.args[0].get_method() == "POST"
                                     for call in self.opener.open.call_args_list), 1)
                self.assertNotIn("accepted", self.output.getvalue())

    def test_public_target_reads_do_not_authorize_dispatch_writes(self):
        failure = urllib.error.HTTPError("https://api.github.com", 403, "PRIVATE ACTIONS WRITE DETAIL", {}, None)
        with self.assertRaises(SystemExit) as raised:
            self.execute(self.responses({**self.project, "visibility": "public"}, self.workflow, self.branch) + [failure])
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET"] * 3 + ["POST"])
        self.assertTrue(all(request.get_header("Authorization") == "Bearer " + self.environment["DISPATCH_TOKEN"]
                            for request in requests))
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertNotIn("accepted", self.output.getvalue())
        self.assertNotIn(self.environment["DISPATCH_TOKEN"], str(raised.exception))

    def test_output_failure_does_not_deny_an_acknowledged_dispatch(self):
        with patch("builtins.print", side_effect=OSError("PRIVATE OUTPUT DETAIL")):
            with self.assertRaisesRegex(SystemExit, "Dispatch acknowledged but output unavailable") as raised:
                self.execute(self.success())
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertEqual(self.opener.open.call_count, 4)

    def test_isolated_cli_works_with_only_copied_dispatch_folder(self):
        environment = {**self.environment, "WORKFLOW_INPUTS": "invalid PRIVATE INPUT"}
        result = subprocess.run([sys.executable, "-I", str(self.bundle / "dispatch.py")],
                                cwd=self.directory, env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("workflow-inputs must be a JSON object", result.stderr)
        self.assertNotIn("ImportError", result.stderr)
        self.assertNotIn("PRIVATE", result.stderr)


if __name__ == "__main__":
    unittest.main()
