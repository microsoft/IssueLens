import contextlib
import copy
import io
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

import yaml


ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "team-memory-post-merge.yml"
ACTION_DIR = ROOT / ".github" / "actions" / "issuelens"
HELPER = ACTION_DIR / "issuelens_action.py"
SPEC = importlib.util.spec_from_file_location("issuelens_action", HELPER)
assert SPEC is not None and SPEC.loader is not None
action = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(action)


class TeamMemoryWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.source, Loader=yaml.BaseLoader)
        cls.job = cls.workflow["jobs"]["reconcile"]
        cls.steps = cls.job["steps"]
        cls.action_metadata = yaml.load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_trusted_base_trigger_and_manual_target(self):
        triggers = self.workflow["on"]
        self.assertNotIn("pull_request", triggers)
        self.assertEqual(triggers["pull_request_target"]["types"], ["closed"])
        target = triggers["workflow_dispatch"]["inputs"]["pull_request_number"]
        self.assertEqual(target["required"], "true")
        self.assertEqual(target["type"], "string")
        self.assertIn("vars.ISSUELENS_TEAM_MEMORY_ENABLED == 'true'", self.job["if"])
        self.assertIn("github.event.repository.default_branch", self.job["if"])
        self.assertIn("github.ref", self.job["if"])
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {
            "contents": "read", "pull-requests": "read", "id-token": "write",
        })

    def test_preflight_precedes_pinned_login_and_submission(self):
        self.assertEqual(self.action_metadata["runs"]["using"], "composite")
        preflight, login, submit = self.action_metadata["runs"]["steps"]
        self.assertEqual(preflight["id"], "preflight")
        self.assertEqual(login["uses"], "azure/login@7ddb5af1ef8758cf1353cf3b42f940aee27ba21c")
        for step in (login, submit):
            self.assertEqual(step["if"], "steps.preflight.outputs.eligible == 'true'")
        self.assertEqual(submit["env"]["AGENT_URL"], "${{ inputs.agent-url }}")
        self.assertEqual(submit["env"]["AGENT_SCOPE"], "${{ inputs.agent-scope }}")
        self.assertEqual(submit["env"]["REQUEST_PATH"], "${{ steps.preflight.outputs.request-path }}")
        self.assertEqual(preflight["env"]["GH_TOKEN"], "${{ inputs.github-token }}")
        self.assertEqual(self.action_metadata["inputs"]["github-token"]["default"], "${{ github.token }}")
        self.assertNotIn("ISSUELENS_AGENT_ENDPOINT", self.source)
        for step, command in ((preflight, "preflight"), (submit, "submit")):
            self.assertEqual(step["shell"], "bash")
            self.assertEqual(step["run"], f'python3 -I "$GITHUB_ACTION_PATH/issuelens_action.py" {command}')
            self.assertEqual(step["env"]["GITHUB_ACTION_PATH"], "${{ github.action_path }}")
            self.assertNotIn("${{", step["run"])
        for name in ("response-path", "wiki-repository", "wiki-sha"):
            self.assertEqual(self.action_metadata["outputs"][name]["value"], "${{ steps.submit.outputs." + name + " }}")
        self.assertEqual(self.action_metadata["outputs"]["status"]["value"], "${{ steps.submit.outputs.status || steps.preflight.outputs.status }}")
        self.assertEqual(preflight["env"]["REQUEST_TYPE"], "${{ inputs.request-type }}")
        self.assertEqual(preflight["env"]["TASK_INPUT"], "${{ inputs.input }}")
        self.assertEqual(preflight["env"]["ISSUE_NUMBER"], "${{ inputs.issue-number }}")
        self.assertEqual(self.action_metadata["inputs"]["request-type"]["required"], "true")
        for step in (preflight, submit):
            self.assertEqual(step["env"]["OUTPUT_MODE"], "${{ inputs.output-mode }}")
            self.assertEqual(step["env"]["SUMMARY_MODE"], "${{ inputs.summary-mode }}")
        self.assertEqual(self.action_metadata["inputs"]["output-mode"]["default"], "hybrid")
        self.assertEqual(self.action_metadata["inputs"]["summary-mode"]["default"], "full")

    def test_concurrency_does_not_coalesce_different_merged_prs(self):
        group = self.workflow["concurrency"]["group"]
        self.assertIn("github.event.pull_request.number", group)
        self.assertIn("inputs.pull_request_number", group)
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(self.job["timeout-minutes"], "20")

    def test_local_caller_loads_only_trusted_action_revision(self):
        checkout, invoke = self.steps
        self.assertEqual(checkout["uses"], "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd")
        self.assertEqual(checkout["with"]["ref"], "${{ github.workflow_sha }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(checkout["with"]["sparse-checkout"], "/.github/actions/issuelens/")
        self.assertEqual(checkout["with"]["sparse-checkout-cone-mode"], "false")
        self.assertEqual(invoke["uses"], "./.github/actions/issuelens")
        self.assertEqual(invoke["with"]["request-type"], "team-memory")
        self.assertEqual(invoke["with"]["agent-url"], "${{ secrets.ISSUELENS_AGENT_URL }}")
        self.assertEqual(invoke["with"]["agent-scope"], "${{ secrets.ISSUELENS_AGENT_SCOPE }}")
        self.assertTrue(set(invoke["with"]).issubset(self.action_metadata["inputs"]))
        self.assertTrue(all("run" not in step for step in self.steps))
        self.assertNotIn("pull_request.head", self.source)
        self.assertNotIn("actions/checkout", json.dumps(self.action_metadata))
        self.assertLess(len(self.source.splitlines()), 65)

    def test_external_caller_example_is_pinned_and_needs_no_checkout(self):
        guide = (ACTION_DIR / "README.md").read_text(encoding="utf-8")
        example = guide.split("```yaml\n", 1)[1].split("```", 1)[0]
        caller = yaml.load(example, Loader=yaml.BaseLoader)
        job = caller["jobs"]["reconcile"]
        self.assertEqual(job["permissions"], self.job["permissions"])
        self.assertEqual(job["timeout-minutes"], "20")
        self.assertIn("ISSUELENS_TEAM_MEMORY_ENABLED", job["if"])
        self.assertIn("pull_request_target", caller["on"])
        self.assertNotIn("actions/checkout", example)
        self.assertEqual(len(job["steps"]), 1)
        invocation = job["steps"][0]
        self.assertEqual(invocation["uses"], "microsoft/IssueLens/.github/actions/issuelens@FULL_COMMIT_SHA")
        self.assertTrue(set(invocation["with"]).issubset(self.action_metadata["inputs"]))
        self.assertIn("placeholder, not a published version", guide)
        for name in self.action_metadata["inputs"]:
            self.assertIn(f"`{name}`", guide)

    def test_agents_do_not_depend_on_the_workflow_contract(self):
        orchestrator = (ROOT / "agents.md").read_text(encoding="utf-8")
        writer = (ROOT / "agents" / "team-memory.md").read_text(encoding="utf-8")
        self.assertIn("Do not assume the request's origin", orchestrator)
        self.assertIn("explicitly supplied", orchestrator)
        self.assertIn("requested response format", orchestrator)
        self.assertIn("skip the acknowledgement", orchestrator)
        self.assertIn("requested response format", writer)
        self.assertIn("scope and constraints", writer)
        for name in ("triage", "find-criticals", "plan", "team-memory"):
            with self.subTest(agent=name):
                content = (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")
                self.assertIn("Do not assume the request's origin", content)
        self.assertIn("Preserve the required critical-issue JSON handoff", (ROOT / "agents" / "find-criticals.md").read_text(encoding="utf-8"))
        for path in ("agents.md", "agents/team-memory.md", ".github/issuelens/team-memory.md",
                     "examples/team-memory.md", "skills/team-memory/SKILL.md", "skills/issuelens-config/SKILL.md"):
            with self.subTest(path=path):
                content = (ROOT / path).read_text(encoding="utf-8")
                for coupled in ("issuelens-team-memory-post-merge/v1", "## Post-merge maintenance jobs",
                                "workflow_dispatch", "workflow_ref", "run_attempt", "postmerge job"):
                    self.assertNotIn(coupled, content, f"Caller-specific contract leaked into {path}")
                for field in ("source_repository", "pull_number", "merge_commit_sha"):
                    self.assertNotIn(f"`{field}`", content, f"Caller result schema leaked into {path}")

    def test_workflow_owns_its_request_and_setup_contract(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        request = action.build_team_memory_request({})["input"]
        for field in ("source_repository", "pull_number", "merge_commit_sha", "wiki_repository", "wiki_sha", "reason"):
            self.assertIn(field, request)
        self.assertIn("Return a final JSON object only", request)
        self.assertIn("add reactions/comments", request)
        self.assertIn("requested response format", readme)
        self.assertIn("ISSUELENS_TEAM_MEMORY_ENABLED=true", readme)
        self.assertIn("pull_request_target: closed", readme)
        self.assertIn("OIDC federation", readme)
        self.assertIn("inspect the mapped wiki/history", readme)
        for path in ("agents.md", "agents/team-memory.md", "README.md", "github_app_mcp/README.md",
                     ".github/copilot-instructions.md", ".github/issuelens/team-memory.md", "examples/team-memory.md"):
            with self.subTest(path=path):
                content = " ".join((ROOT / path).read_text(encoding="utf-8").split())
                self.assertFalse(any(stale in content for stale in (
                    "skeleton does not submit", "skeleton is not functional", "orchestration remains incomplete",
                    "trusted postmerge job",
                )), f"Stale automation limitation in {path}")


class Response(io.BytesIO):
    def __init__(self, body, content_type="application/json"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class TeamMemoryActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = pathlib.Path(temporary.name)
        self.merge_sha = "a" * 40
        self.project = {"id": 100, "full_name": "example/project", "default_branch": "main"}
        self.pull = {
            "number": 27, "state": "closed", "merged": True,
            "merge_commit_sha": self.merge_sha, "merged_at": "2026-09-10T00:00:00Z",
            "base": {"ref": "main", "repo": self.project},
            "head": {"ref": "untrusted", "repo": {"full_name": "contributor/fork"}},
            "body": "UNTRUSTED_PR_TEXT $(touch unsafe)\n::error::forged",
        }
        self.event = {"action": "closed", "number": 27, "repository": self.project, "pull_request": self.pull}
        self.environment = {
            "OUTPUT_MODE": "quiet", "SUMMARY_MODE": "status",
            "REQUEST_TYPE": "team-memory",
            "GITHUB_REPOSITORY": "example/project", "GITHUB_EVENT_NAME": "pull_request_target",
            "GITHUB_EVENT_PATH": str(self.directory / "event.json"),
            "GITHUB_REF": "refs/heads/main", "GITHUB_ACTOR": "maintainer",
            "GITHUB_TRIGGERING_ACTOR": "rerunner", "GITHUB_RUN_ID": "123456",
            "GITHUB_RUN_ATTEMPT": "2", "GITHUB_WORKFLOW_SHA": "b" * 40,
            "GITHUB_WORKFLOW_REF": "example/project/.github/workflows/team-memory-post-merge.yml@refs/heads/main",
            "RUNNER_TEMP": str(self.directory), "GITHUB_OUTPUT": str(self.directory / "output.txt"),
            "REQUEST_PATH": str(self.directory / "request.json"),
            "GITHUB_STEP_SUMMARY": str(self.directory / "summary.md"), "DISPATCH_PR": "27",
            "GH_TOKEN": "fake-repository-token", "AGENT_SCOPE": "https://ai.azure.com/.default",
            "AGENT_URL": "https://test.services.ai.azure.com/api/projects/test/agents/test/endpoint/protocols/invocations?api-version=v1",
        }
        self.envelope = {
            "request_type": "team-memory",
            "metadata": {"repository": "example/project", "pull_number": 27, "merge_commit_sha": self.merge_sha},
            "request": {"input": "Authorized fixture task"},
        }
        self.result = {
            "status": "updated", "source_repository": "example/project", "pull_number": 27,
            "merge_commit_sha": self.merge_sha, "wiki_repository": "example/knowledge",
            "wiki_sha": "c" * 40, "reason": "Tool-confirmed update",
        }
        self.output = io.StringIO()

    def execute(self, command, responses, token_result="fake-endpoint-token\n"):
        pathlib.Path(self.environment["GITHUB_EVENT_PATH"]).write_text(json.dumps(self.event), encoding="utf-8")
        self.opener = Mock()
        self.opener.open.side_effect = responses
        self.token = Mock(return_value=token_result)
        with patch.dict(os.environ, self.environment, clear=True), contextlib.redirect_stdout(self.output), \
                patch("urllib.request.build_opener", return_value=self.opener) as builder, \
                patch("subprocess.check_output", self.token):
            self.builder = builder
            action.run(command)

    def github_responses(self):
        return [Response(json.dumps(value).encode()) for value in (self.project, self.pull)]

    def write_envelope(self):
        pathlib.Path(self.environment["REQUEST_PATH"]).write_text(json.dumps(self.envelope), encoding="utf-8")

    def action_outputs(self):
        return dict(line.split("=", 1) for line in (self.directory / "output.txt").read_text().splitlines())

    def prepared_envelope(self):
        return json.loads(pathlib.Path(self.action_outputs()["request-path"]).read_text())

    def stream(self, result=None, done=True, before=""):
        event = {"type": "assistant.message", "data": {"content": json.dumps(result or self.result)}}
        body = before + ": heartbeat\n\n" + "data: " + json.dumps(event) + "\n\n"
        if done:
            body += 'event: done\ndata: {"invocation_id":"fixture","session_id":"fixture"}\n\n'
        return Response(body.encode(), "text/event-stream")

    def test_merged_fork_is_validated_without_fetching_fork_content(self):
        self.execute("preflight", self.github_responses())
        envelope = self.prepared_envelope()
        metadata = envelope["metadata"]
        self.assertEqual(metadata["merge_commit_sha"], self.merge_sha)
        self.assertEqual(metadata["source_identity"], f"example/project#27:{self.merge_sha}")
        self.assertEqual(metadata["run_attempt"], 2)
        self.assertEqual(metadata["triggering_actor"], "rerunner")
        self.assertNotIn("contract", metadata)
        self.assertEqual(set(envelope["request"]), {"input"})
        self.assertIn("This request comes from", envelope["request"]["input"])
        self.assertIn("context, not independent authorization proof", envelope["request"]["input"])
        self.assertNotIn("UNTRUSTED_PR_TEXT", envelope["request"]["input"])
        self.assertNotIn("contributor/fork", envelope["request"]["input"])
        self.assertNotIn("fake-repository-token", json.dumps(envelope))
        requests = self.opener.open.call_args_list
        self.assertEqual([call.args[0].full_url for call in requests], [
            "https://api.github.com/repos/example/project", "https://api.github.com/repos/example/project/pulls/27",
        ])
        self.assertTrue(all(call.kwargs["timeout"] == 30 for call in requests))
        self.assertEqual(self.action_outputs()["eligible"], "true")
        self.assertEqual(pathlib.Path(self.action_outputs()["request-path"]).parent, self.directory)
        self.token.assert_not_called()

    def test_manual_dispatch_uses_explicit_merged_pr(self):
        self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.event = {"repository": self.project}
        self.execute("preflight", self.github_responses())
        envelope = self.prepared_envelope()
        self.assertEqual(envelope["metadata"]["event_name"], "workflow_dispatch")
        self.assertEqual(envelope["metadata"]["event_action"], "workflow_dispatch")

    def test_invalid_dispatch_numbers_fail_before_github_or_login(self):
        self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        for number in ("", "0", "-1", "01", "1;echo unsafe", "1\n2", "1e3", "9" * 16):
            with self.subTest(number=number):
                self.environment["DISPATCH_PR"] = number
                with self.assertRaises(SystemExit):
                    self.execute("preflight", [])
                self.opener.open.assert_not_called()
                self.token.assert_not_called()

    def test_rejects_unmerged_and_unsupported_events(self):
        for mutation in ("unmerged", "reopened", "pull_request", "wrong_event_repository"):
            with self.subTest(mutation=mutation):
                original = copy.deepcopy(self.event)
                if mutation == "unmerged":
                    self.event["pull_request"]["merged"] = False
                elif mutation == "reopened":
                    self.event["action"] = "reopened"
                elif mutation == "pull_request":
                    self.environment["GITHUB_EVENT_NAME"] = "pull_request"
                else:
                    self.event["repository"]["full_name"] = "other/project"
                with self.assertRaises(SystemExit):
                    self.execute("preflight", [])
                self.opener.open.assert_not_called()
                self.event = original
                self.environment["GITHUB_EVENT_NAME"] = "pull_request_target"

    def test_authoritative_pr_and_workflow_must_match(self):
        original_pull = copy.deepcopy(self.pull)
        original_environment = self.environment.copy()
        for mutation in ("unmerged", "wrong_base", "wrong_repository", "short_sha", "missing_date",
                         "event_sha", "workflow_path", "workflow_sha", "dispatch_branch"):
            with self.subTest(mutation=mutation):
                self.pull = copy.deepcopy(original_pull)
                self.event["pull_request"] = copy.deepcopy(original_pull)
                self.environment = original_environment.copy()
                if mutation == "unmerged":
                    self.pull["merged"] = False
                elif mutation == "wrong_base":
                    self.pull["base"]["ref"] = "release"
                elif mutation == "wrong_repository":
                    self.pull["base"]["repo"]["id"] = 200
                elif mutation == "short_sha":
                    self.pull["merge_commit_sha"] = "abc123"
                elif mutation == "missing_date":
                    self.pull["merged_at"] = None
                elif mutation == "event_sha":
                    self.event["pull_request"]["merge_commit_sha"] = "d" * 40
                elif mutation == "workflow_path":
                    self.environment["GITHUB_WORKFLOW_REF"] = "other/project/.github/workflows/untrusted.yml@refs/heads/main"
                elif mutation == "workflow_sha":
                    self.environment["GITHUB_WORKFLOW_SHA"] = "short"
                else:
                    self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
                    self.environment["GITHUB_REF"] = "refs/heads/untrusted"
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.github_responses())
                self.assertFalse((self.directory / "output.txt").exists())
                self.assertEqual(list(self.directory.glob("issuelens-request-*.json")), [])

    def test_github_failure_does_not_leak_details_or_submit(self):
        with self.assertRaises(SystemExit) as raised:
            self.execute("preflight", [urllib.error.HTTPError("https://api.github.com", 403, "private-detail", {}, None)])
        self.assertNotIn("private-detail", str(raised.exception))
        self.token.assert_not_called()
        self.assertFalse((self.directory / "output.txt").exists())

    def test_successful_submission_and_no_change(self):
        self.write_envelope()
        for status in ("updated", "no-change"):
            with self.subTest(status=status):
                self.result["status"] = status
                self.execute("submit", [self.stream()])
                request = self.opener.open.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.data), self.envelope["request"])
                self.assertEqual(request.get_header("Authorization"), "Bearer fake-endpoint-token")
                self.assertNotIn(b"fake-endpoint-token", request.data)
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(self.token.call_args.args[0], [
                    "az", "account", "get-access-token", "--scope", "https://ai.azure.com/.default",
                    "--query", "accessToken", "-o", "tsv",
                ])
                summary = (self.directory / "summary.md").read_text()
                self.assertIn(self.result["wiki_sha"], summary)
                self.assertNotIn("fake-endpoint-token", summary)
                self.assertEqual(self.action_outputs(), {
                    "status": status, "wiki-repository": self.result["wiki_repository"],
                    "wiki-sha": self.result["wiki_sha"],
                })
                self.assertIsNone(self.builder.call_args.args[0].redirect_request(None, None, None, None, None, None))

    def test_workflow_name_is_not_hard_coded(self):
        for name in ("maintenance.yml", "wiki-refresh.yaml"):
            with self.subTest(name=name):
                self.environment["GITHUB_WORKFLOW_REF"] = f"example/project/.github/workflows/{name}@refs/heads/main"
                self.execute("preflight", self.github_responses())
                self.assertEqual(self.prepared_envelope()["metadata"]["workflow_ref"], self.environment["GITHUB_WORKFLOW_REF"])

    def test_workflow_source_still_requires_own_default_branch(self):
        for reference in (
            "example/project/.github/workflows/maintenance.yml@refs/heads/untrusted",
            "example/project/.github/workflows/../maintenance.yml@refs/heads/main",
            "example/project/.github/actions/maintenance.yml@refs/heads/main",
            "microsoft/IssueLens/.github/workflows/maintenance.yml@refs/heads/main",
        ):
            with self.subTest(reference=reference):
                self.environment["GITHUB_WORKFLOW_REF"] = reference
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.github_responses())
                self.assertFalse((self.directory / "output.txt").exists())

    def test_repeated_action_calls_use_separate_request_files(self):
        self.execute("preflight", self.github_responses())
        first_path = self.action_outputs()["request-path"]
        first_content = pathlib.Path(first_path).read_text()
        self.environment["GITHUB_RUN_ATTEMPT"] = "3"
        self.execute("preflight", self.github_responses())
        self.assertNotEqual(self.action_outputs()["request-path"], first_path)
        self.assertEqual(pathlib.Path(first_path).read_text(), first_content)
        self.assertEqual(self.prepared_envelope()["metadata"]["run_attempt"], 3)

    def test_helper_cli_runs_outside_the_repository_in_isolated_mode(self):
        self.environment["GITHUB_EVENT_NAME"] = "pull_request"
        pathlib.Path(self.environment["GITHUB_EVENT_PATH"]).write_text(json.dumps(self.event), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-I", str(HELPER), "preflight"],
            cwd=self.directory, env=self.environment, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unsupported event", result.stderr)
        self.assertNotIn("ImportError", result.stderr)
        self.assertFalse((self.directory / "output.txt").exists())

    def test_azure_token_is_required_before_submission(self):
        self.write_envelope()
        for token in ("", "token\nsecond-token", "token with spaces"):
            with self.subTest(token=token), self.assertRaises(SystemExit):
                self.execute("submit", [], token_result=token)
            self.opener.open.assert_not_called()

    def test_stream_total_budget_and_deadline(self):
        self.write_envelope()
        self.execute("submit", [self.stream()])
        read_result = action.read_response
        body = (b":" + b"x" * (128 * 1024) + b"\n\n") * 65
        with Response(body, "text/event-stream") as response:
            with self.assertRaisesRegex(ValueError, "stream limits"):
                read_result(response)
        with self.stream() as response, patch("time.monotonic", side_effect=[0, 901]):
            with self.assertRaisesRegex(ValueError, "read budget"):
                read_result(response)

    def test_rejects_incorrect_endpoint_before_token_acquisition(self):
        self.write_envelope()
        for url in ("", "http://test.services.ai.azure.com/protocols/invocations",
                    "https://evil.test/protocols/invocations", "https://user@test.services.ai.azure.com/protocols/invocations",
                    "https://test.services.ai.azure.com/protocols/openai/responses",
                    "https://test.services.ai.azure.com:444/protocols/invocations"):
            with self.subTest(url=url):
                self.environment["AGENT_URL"] = url
                with self.assertRaises(SystemExit):
                    self.execute("submit", [])
                self.token.assert_not_called()
                self.opener.open.assert_not_called()

    def test_stream_errors_and_truncation_never_report_success(self):
        self.write_envelope()
        streams = [
            self.stream(done=False),
            self.stream(before='data: {"type":"error","message":"private-detail"}\n\n'),
            self.stream(before='data: {"type":"session.error"}\n\n'),
            Response(b'{"status":"ok"}', "application/json"),
            Response(b"event: done\ndata: {}\n\n", "text/event-stream"),
            Response(b"data: invalid-json\n\n", "text/event-stream"),
            Response(b"data: " + b"a" * (1024 * 1024 + 1), "text/event-stream"),
            urllib.error.HTTPError(self.environment["AGENT_URL"], 500, "private-detail", {}, None),
        ]
        for response in streams:
            with self.subTest(response=response):
                with self.assertRaises(SystemExit) as raised:
                    self.execute("submit", [response])
                self.assertNotIn("private-detail", str(raised.exception))
                self.assertEqual(self.opener.open.call_count, 1)
                summary = (self.directory / "summary.md").read_text()
                self.assertIn("Invocation failed or outcome unknown", summary)
                self.assertNotIn("private-detail", summary)
                self.assertNotIn("Agent response", summary)
                self.assertFalse((self.directory / "output.txt").exists())

    def test_failed_ambiguous_or_wrong_target_results_fail_job(self):
        self.write_envelope()
        for changes in ({"status": "failed"}, {"status": "needs-review"}, {"status": "unknown"},
                        {"source_repository": "other/project"}, {"pull_number": 28}, {"pull_number": True},
                        {"merge_commit_sha": "d" * 40}, {"wiki_sha": None}, {"wiki_sha": "abc"},
                        {"wiki_repository": "https://evil.test"}, {"reason": None}, {"reason": " "},
                        {"reason": "x" * 4097}):
            with self.subTest(changes=changes), self.assertRaises(SystemExit):
                self.execute("submit", [self.stream({**self.result, **changes})])
        self.assertIn("Invocation failed or outcome unknown", (self.directory / "summary.md").read_text())
        self.assertFalse((self.directory / "output.txt").exists())

    def test_malformed_completion_and_duplicate_result_keys_fail(self):
        self.write_envelope()
        result_json = json.dumps(self.result)
        for content, completion in (
            (result_json, {}),
            (result_json, {"invocation_id": "fixture", "session_id": None}),
            ('{"status":"failed",' + result_json[1:], {"invocation_id": "fixture", "session_id": "fixture"}),
        ):
            event = {"type": "assistant.message", "data": {"content": content}}
            stream = f"data: {json.dumps(event)}\n\nevent: done\ndata: {json.dumps(completion)}\n\n"
            with self.subTest(completion=completion), self.assertRaises(SystemExit):
                self.execute("submit", [Response(stream.encode(), "text/event-stream")])
        self.assertIn("Invocation failed or outcome unknown", (self.directory / "summary.md").read_text())
        self.assertFalse((self.directory / "output.txt").exists())


if __name__ == "__main__":
    unittest.main()
