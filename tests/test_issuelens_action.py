import copy
import json
from pathlib import Path
import unittest

import yaml

import test_team_memory_workflow as memory_tests

Response = memory_tests.Response
action = memory_tests.action


class IssueLensRequestTests(unittest.TestCase):
    execute = memory_tests.TeamMemoryActionTests.execute
    write_envelope = memory_tests.TeamMemoryActionTests.write_envelope
    action_outputs = memory_tests.TeamMemoryActionTests.action_outputs
    prepared_envelope = memory_tests.TeamMemoryActionTests.prepared_envelope

    def setUp(self):
        memory_tests.TeamMemoryActionTests.setUp(self)
        self.environment["REQUEST_TYPE"] = "issue-loop"
        self.environment["GITHUB_EVENT_NAME"] = "issues"
        self.environment["ISSUE_NUMBER"] = "27"
        self.environment["TASK_INPUT"] = ""
        self.issue = {"number": 27, "author_association": "NONE", "body": "UNTRUSTED_ISSUE_TEXT"}
        self.event = {
            "action": "opened", "repository": self.project, "issue": self.issue,
            "sender": {"login": "reporter", "type": "User"},
        }

    def test_issue_events_preserve_neutral_handoff(self):
        self.execute("preflight", [Response(json.dumps(self.project).encode())])
        envelope = self.prepared_envelope()
        self.assertEqual(envelope["request_type"], "issue-loop")
        metadata = envelope["metadata"]
        self.assertEqual(metadata["issue_number"], 27)
        self.assertEqual(metadata["actor_login"], "reporter")
        self.assertIsNone(metadata["comment_id"])
        self.assertFalse(metadata["comment_added"])
        self.assertFalse(metadata["manual_dispatch"])
        self.assertIn("global built-in command and trusted issue-loop contracts", envelope["request"]["input"])
        self.assertNotIn("UNTRUSTED_ISSUE_TEXT", json.dumps(envelope))
        self.assertNotIn("triage-result", envelope["request"]["input"])

    def test_manual_issue_dispatch_uses_explicit_issue_and_null_comment(self):
        self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.event = {"repository": self.project}
        self.execute("preflight", [Response(json.dumps(value).encode()) for value in (self.project, self.issue)])
        metadata = self.prepared_envelope()["metadata"]
        self.assertTrue(metadata["manual_dispatch"])
        self.assertIsNone(metadata["comment_id"])
        self.assertIsNone(metadata["comment_author_association"])
        self.assertFalse(metadata["comment_added"])
        self.assertEqual(metadata["actor_login"], self.environment["GITHUB_ACTOR"])
        self.assertEqual(self.opener.open.call_args.args[0].full_url, "https://api.github.com/repos/example/project/issues/27")

    def test_manual_dispatch_rejects_pr_as_issue(self):
        self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.event = {"repository": self.project}
        self.issue["pull_request"] = {"url": "unused"}
        self.execute("preflight", [Response(json.dumps(value).encode()) for value in (self.project, self.issue)])
        self.assertEqual(self.action_outputs()["status"], "skipped")
        self.assertEqual(self.action_outputs()["skip-reason"], "pull_request_issue")
        self.token.assert_not_called()

    def test_unsupported_issue_event_actions_skip_without_network(self):
        for event_name, event_action in (("issues", "edited"), ("issues", "closed"),
                                         ("issue_comment", "deleted"), ("push", "opened")):
            with self.subTest(event_name=event_name, event_action=event_action):
                self.environment["GITHUB_EVENT_NAME"] = event_name
                self.event["action"] = event_action
                self.execute("preflight", [])
                self.assertEqual(self.action_outputs()["status"], "skipped")
                self.opener.open.assert_not_called()

    def test_invalid_issue_or_comment_identifiers_fail_without_network(self):
        for invalid in (None, False, 0, "-1", "1; echo unsafe", "1\n2"):
            with self.subTest(invalid=invalid):
                self.issue["number"] = invalid
                with self.assertRaises(SystemExit):
                    self.execute("preflight", [])
                self.opener.open.assert_not_called()
        self.issue["number"] = 27
        self.environment["GITHUB_EVENT_NAME"] = "issue_comment"
        self.event["action"] = "created"
        self.event["comment"] = {"id": 0, "user": {"type": "User"}}
        with self.assertRaises(SystemExit):
            self.execute("preflight", [])
        self.opener.open.assert_not_called()

    def test_all_request_types_require_own_default_branch(self):
        for request_type in ("issue-loop", "team-memory", "task"):
            with self.subTest(request_type=request_type):
                self.environment["REQUEST_TYPE"] = request_type
                self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
                self.environment["GITHUB_REF"] = "refs/heads/untrusted"
                self.environment["TASK_INPUT"] = "Summarize example/project#27" if request_type == "task" else ""
                self.event = {"repository": self.project}
                with self.assertRaises(SystemExit):
                    self.execute("preflight", [Response(json.dumps(self.project).encode())])
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_invalid_or_ambiguous_adapter_inputs_fail_before_network(self):
        for request_type, task in (("", ""), ("planning", ""), ("TASK", ""),
                                   ("task", " "), ("task", "\u754c" * (64 * 1024 // 3 + 1)),
                                   ("issue-loop", "Override event"), ("team-memory", "Override event")):
            with self.subTest(request_type=request_type, length=len(task)):
                self.environment["REQUEST_TYPE"] = request_type
                self.environment["TASK_INPUT"] = task
                with self.assertRaises(SystemExit):
                    self.execute("preflight", [])
                self.opener.open.assert_not_called()
                self.token.assert_not_called()

    def test_comment_metadata_preserves_command_validation_inputs(self):
        self.environment["GITHUB_EVENT_NAME"] = "issue_comment"
        self.event["comment"] = {
            "id": 99, "user": {"login": "maintainer", "type": "User"},
            "author_association": "MEMBER", "body": "@issuelens plan UNTRUSTED_COMMENT_TEXT",
        }
        self.event["sender"] = {"login": "maintainer", "type": "User"}
        for event_action in ("created", "edited"):
            with self.subTest(event_action=event_action):
                self.event["action"] = event_action
                self.execute("preflight", [Response(json.dumps(self.project).encode())])
                metadata = self.prepared_envelope()["metadata"]
                self.assertEqual(metadata["comment_id"], 99)
                self.assertEqual(metadata["comment_author_login"], "maintainer")
                self.assertEqual(metadata["comment_author_association"], "MEMBER")
                self.assertEqual(metadata["comment_added"], event_action == "created")
                self.assertEqual(metadata["comment_edited"], event_action == "edited")
                self.assertNotIn("UNTRUSTED_COMMENT_TEXT", json.dumps(self.prepared_envelope()))

    def test_bot_and_pr_comments_skip_before_network_or_login(self):
        self.environment["GITHUB_EVENT_NAME"] = "issue_comment"
        self.event["action"] = "created"
        self.event["comment"] = {"id": 99, "user": {"login": "reporter", "type": "User"}}
        original = copy.deepcopy(self.event)
        for invalid in ("bot_actor", "bot_author", "pull_request"):
            with self.subTest(invalid=invalid):
                self.event = copy.deepcopy(original)
                if invalid == "bot_actor":
                    self.event["sender"]["type"] = "Bot"
                elif invalid == "bot_author":
                    self.event["comment"]["user"]["type"] = "Bot"
                else:
                    self.event["issue"]["pull_request"] = {"url": "unused"}
                self.execute("preflight", [])
                self.assertEqual(self.action_outputs()["eligible"], "false")
                self.opener.open.assert_not_called()
                self.token.assert_not_called()

    def test_direct_task_has_no_synthesized_issue_loop_authority(self):
        self.environment["REQUEST_TYPE"] = "task"
        self.environment["TASK_INPUT"] = "Plan example/project#27 without posting comments."
        self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.event = {"repository": self.project}
        self.execute("preflight", [Response(json.dumps(self.project).encode())])
        envelope = self.prepared_envelope()
        self.assertEqual(envelope["request_type"], "task")
        self.assertIn(self.environment["TASK_INPUT"], envelope["request"]["input"])
        self.assertNotIn("Trusted event metadata:", envelope["request"]["input"])
        self.assertNotIn("comment_added", json.dumps(envelope))
        self.assertIn("does not carry trusted issue-loop provenance", envelope["request"]["input"])

    def test_generic_completion_preserves_text_without_wiki_schema(self):
        self.envelope["request_type"] = "task"
        self.write_envelope()
        text = "Action Plan\nInvestigate the parser.\nDesign Specification\nPreserve the interface."
        event = {"type": "assistant.message", "data": {"content": text}}
        stream = f'data: {json.dumps(event)}\n\nevent: done\ndata: {{"invocation_id":"fixture","session_id":"fixture"}}\n\n'
        self.execute("submit", [Response(stream.encode(), "text/event-stream")])
        outputs = self.action_outputs()
        self.assertEqual(outputs["status"], "completed")
        self.assertNotIn("wiki-sha", outputs)
        self.assertNotIn("Action Plan", self.output.getvalue())
        self.assertEqual(Path(outputs["response-path"]).read_text(encoding="utf-8"), text)

    def test_generic_outcomes_do_not_claim_business_success(self):
        for request_type in ("issue-loop", "task"):
            for text in ("No action: nothing new.", "Planning is blocked pending evidence.",
                         '{"status":"needs-review"}', "::error::Do not execute this text"):
                with self.subTest(request_type=request_type, text=text):
                    self.envelope["request_type"] = request_type
                    self.write_envelope()
                    event = {"type": "assistant.message", "data": {"content": text}}
                    stream = f'data: {json.dumps(event)}\n\nevent: done\ndata: {{"invocation_id":"fixture","session_id":"fixture"}}\n\n'
                    self.execute("submit", [Response(stream.encode(), "text/event-stream")])
                    outputs = self.action_outputs()
                    self.assertEqual(outputs["status"], "completed")
                    self.assertEqual(Path(outputs["response-path"]).read_text(encoding="utf-8"), text)
                    self.assertNotIn(text, self.output.getvalue())
                    self.assertIn("does not assert", (self.directory / "summary.md").read_text())

    def test_generic_transport_failure_or_unknown_adapter_never_succeeds(self):
        for request_type in ("issue-loop", "task", "unknown"):
            for body in (b'data: {"type":"error","message":"private-detail"}\n\n',
                         b'data: {"type":"assistant.message","data":{"content":"Partial answer"}}\n\n'):
                with self.subTest(request_type=request_type):
                    self.envelope["request_type"] = request_type
                    self.write_envelope()
                    with self.assertRaises(SystemExit) as raised:
                        self.execute("submit", [Response(body, "text/event-stream")])
                    self.assertNotIn("private-detail", str(raised.exception))
                    self.assertFalse((self.directory / "output.txt").exists())
                    self.assertFalse(list(self.directory.glob("issuelens-response-*")))
                    if request_type == "unknown":
                        self.token.assert_not_called()

    def test_subagent_messages_cannot_be_the_final_response(self):
        from copilot.generated.session_events import AssistantMessageData
        nested_data = AssistantMessageData(
            content=json.dumps(self.result), message_id="nested", parent_tool_call_id="task-call",
        ).to_dict()
        nested_events = [
            {"type": "assistant.message", "data": nested_data},
            {"type": "assistant.message", "agentId": "subagent-id", "data": {"content": json.dumps(self.result)}},
        ]
        done = 'event: done\ndata: {"invocation_id":"fixture","session_id":"fixture"}\n\n'
        for nested in nested_events:
            encoded = "data: " + json.dumps(nested) + "\n\n"
            with self.subTest(nested=nested), Response((encoded + done).encode(), "text/event-stream") as response:
                with self.assertRaisesRegex(ValueError, "no final response"):
                    action.read_response(response)
            root = {"type": "assistant.message", "data": {"content": "Root answer"}}
            body = "data: " + json.dumps(root) + "\n\n" + encoded + done
            with Response(body.encode(), "text/event-stream") as response:
                self.assertEqual(action.read_response(response), "Root answer")
        tool_message = {"type": "assistant.message", "data": {"content": "Starting", "toolRequests": [{"name": "task"}]}}
        with Response(("data: " + json.dumps(tool_message) + "\n\n" + done).encode(), "text/event-stream") as response:
            with self.assertRaisesRegex(ValueError, "no final response"):
                action.read_response(response)

    def test_prepared_generic_requests_flow_through_shared_submit(self):
        for request_type in ("issue-loop", "task"):
            with self.subTest(request_type=request_type):
                self.environment["REQUEST_TYPE"] = request_type
                self.environment["TASK_INPUT"] = "Summarize example/project#27" if request_type == "task" else ""
                self.execute("preflight", [Response(json.dumps(self.project).encode())])
                prepared = self.prepared_envelope()
                self.environment["REQUEST_PATH"] = self.action_outputs()["request-path"]
                (self.directory / "output.txt").unlink()
                event = {"type": "assistant.message", "data": {"content": "No action needed."}}
                body = f'data: {json.dumps(event)}\n\nevent: done\ndata: {{"invocation_id":"fixture","session_id":"fixture"}}\n\n'
                self.execute("submit", [Response(body.encode(), "text/event-stream")])
                self.assertEqual(json.loads(self.opener.open.call_args.args[0].data), prepared["request"])
                self.assertEqual(self.action_outputs()["status"], "completed")
                self.assertEqual(self.opener.open.call_count, 1)
                self.token.assert_called_once()


class IssueLensActionDocumentationTests(unittest.TestCase):
    def test_examples_cover_every_request_type_and_declared_input(self):
        guide = (memory_tests.ACTION_DIR / "README.md").read_text(encoding="utf-8")
        metadata = yaml.load((memory_tests.ACTION_DIR / "action.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        examples = [part.split("```", 1)[0] for part in guide.split("```yaml\n")[1:]]
        memory_workflow, issue_steps, task_steps = [yaml.load(example, Loader=yaml.BaseLoader) for example in examples]
        invocations = [memory_workflow["jobs"]["reconcile"]["steps"][0], issue_steps[0], task_steps[0]]
        self.assertEqual({step["with"]["request-type"] for step in invocations}, action.REQUEST_TYPES)
        for invocation in invocations:
            self.assertEqual(invocation["uses"], "microsoft/IssueLens/.github/actions/issuelens@FULL_COMMIT_SHA")
            self.assertTrue(set(invocation["with"]).issubset(metadata["inputs"]))
        for output in metadata["outputs"]:
            self.assertIn(f"`{output}`", guide)
        self.assertIn("transport completion, not business success", guide)
        self.assertIn("never pass public issue/PR/comment text", guide)
        self.assertFalse((memory_tests.ROOT / ".github" / "actions" / "team-memory").exists())


if __name__ == "__main__":
    unittest.main()
