import io
import json
import unittest
from unittest.mock import patch

import test_team_memory_workflow as action_tests


def message(kind, content, message_id="message", **extra):
    field = "deltaContent" if kind == "assistant.message_delta" else "content"
    return {"type": kind, "data": {"messageId": message_id, field: content}, **extra}


class StreamOutputTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.now = 0.0
        self.renderer = action_tests.action.StreamRenderer(
            stream=self.output, clock=lambda: self.now, secrets=("private-token",),
        )

    def test_streams_lines_before_completion_and_deduplicates_final(self):
        self.renderer.event(message("assistant.message_delta", "Checking "))
        self.assertEqual(self.output.getvalue(), "")
        self.renderer.event(message("assistant.streaming_delta", "Checking "))
        self.renderer.event(message("assistant.message_delta", "the issue.\n"))
        self.assertIn("[IssueLens] Checking the issue.", self.output.getvalue())
        self.renderer.event(message("assistant.message", "Checking the issue.\n"))
        self.assertEqual(self.output.getvalue().count("Checking the issue."), 1)

    def test_periodically_flushes_partial_text_and_completion_only_fallback(self):
        self.renderer.event(message("assistant.message_delta", "A paragraph is arriving without a newline yet."))
        self.now = 2
        self.renderer.tick()
        self.assertIn("A paragraph", self.output.getvalue())
        self.renderer.event(message("assistant.message", "A paragraph is arriving without a newline yet."))
        self.renderer.event(message("assistant.message", "Final answer.", "second"))
        self.assertEqual(self.output.getvalue().count("Final answer."), 1)

    def test_nested_text_and_tools_are_labelled_without_payloads(self):
        self.renderer.event({"type": "subagent.started", "data": {"agentName": "plan", "toolCallId": "task1"}})
        self.renderer.event({"type": "assistant.message_delta", "data": {
            "messageId": "nested", "parentToolCallId": "task1", "deltaContent": "Inspecting tests.\n",
        }})
        self.renderer.event({"type": "tool.execution_start", "data": {
            "toolCallId": "read1", "parentToolCallId": "task1", "toolName": "github-get_file",
            "arguments": {"body": "PRIVATE ARGUMENT"},
        }})
        self.now = 3
        self.renderer.event({"type": "tool.execution_complete", "data": {
            "toolCallId": "read1", "parentToolCallId": "task1", "success": True,
            "result": {"content": "PRIVATE RESULT"},
        }})
        output = self.output.getvalue()
        self.assertIn("[plan] Inspecting tests.", output)
        self.assertIn("github-get_file started", output)
        self.assertIn("github-get_file completed (3.0s)", output)
        self.assertNotIn("PRIVATE", output)

    def test_hides_internal_events_and_reports_retry_without_raw_error(self):
        for kind in ("assistant.reasoning", "assistant.reasoning_delta", "assistant.tool_call_delta",
                     "assistant.streaming_delta", "permission.requested", "session.usage_info", "system.message"):
            self.renderer.event(message(kind, "INTERNAL ONLY"))
        self.renderer.event({"type": "model.call_failure", "data": {"error": "PRIVATE ERROR"}})
        self.renderer.event({"type": "assistant.turn_retry", "data": {"reason": "PRIVATE ERROR"}})
        self.renderer.event(message("assistant.message", "Recovered answer."))
        self.assertNotIn("INTERNAL", self.output.getvalue())
        self.assertNotIn("PRIVATE", self.output.getvalue())
        self.assertIn("retry", self.output.getvalue().lower())
        self.assertIn("Recovered answer.", self.output.getvalue())

    def test_untrusted_text_cannot_inject_runner_commands_or_terminal_controls(self):
        self.renderer.event(message("assistant.message_delta", "private-"))
        self.now = 2
        self.renderer.tick()
        self.renderer.event(message("assistant.message_delta", "token\n::error::forged\r\x1b[31mtext\x08\n"))
        output = self.output.getvalue()
        self.assertNotIn("private-token", output)
        self.assertNotIn("::error::", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\r", output)
        self.assertNotIn("\x08", output)
        self.assertIn("***", output)

    def test_activity_and_quiet_modes_do_not_publish_assistant_text(self):
        for mode in ("activity", "quiet"):
            output = io.StringIO()
            renderer = action_tests.action.StreamRenderer(mode=mode, stream=output)
            renderer.event(message("assistant.message", "PRIVATE ANSWER"))
            renderer.event({"type": "tool.execution_start", "data": {"toolCallId": "1", "toolName": "get_file"}})
            renderer.finish("completed")
            self.assertNotIn("PRIVATE ANSWER", output.getvalue())
            if mode == "activity":
                self.assertIn("get_file", output.getvalue())
            else:
                self.assertEqual(output.getvalue(), "")

    def test_redacts_known_secrets_reconstructed_by_control_normalization(self):
        for separator in ("\x1b[31m", "\u200b", "\x00", "\x1b]0;title\x07"):
            with self.subTest(separator=repr(separator)):
                text = "private-" + separator + "token\n"
                self.renderer.event(message("assistant.message", text, repr(separator)))
                self.assertNotIn("private-token", self.output.getvalue())
                self.assertNotIn("private-token", self.renderer.summary("completed", text=text))
        self.assertIn("***", self.output.getvalue())

    def test_secret_masking_survives_long_controls_split_across_chunks(self):
        for sequence, ending in (("\x1b[" + "0" * 2000, "m"), ("\x1b]" + "x" * 2000, "\x07"),
                                 ("\u200b" * 2000, "")):
            output = io.StringIO()
            renderer = action_tests.action.StreamRenderer(stream=output, clock=lambda: self.now, secrets=("private-token",))
            renderer.event(message("assistant.message_delta", "private-" + sequence))
            self.now += 2
            renderer.tick()
            self.assertNotIn("private-", output.getvalue())
            renderer.event(message("assistant.message_delta", ending + "token\n"))
            self.assertNotIn("private-token", output.getvalue())
            self.assertNotIn("token", output.getvalue())
            self.assertIn("***", output.getvalue())

    def test_display_io_failure_degrades_to_quiet(self):
        class UnavailableOutput:
            def write(self, text):
                raise OSError("private filesystem details")

        renderer = action_tests.action.StreamRenderer(stream=UnavailableOutput())
        renderer.event(message("assistant.message", "Answer"))
        renderer.finish("completed")
        self.assertTrue(renderer.io_failed)
        self.assertEqual(renderer.mode, "quiet")
        self.assertIn("Live display became unavailable", renderer.summary("completed", text="Answer"))

    def test_analysis_phase_is_not_user_facing_content(self):
        for kind in ("assistant.message", "assistant.message_delta"):
            event = message(kind, "PRIVATE ANALYSIS\n")
            event["data"]["phase"] = "analysis"
            self.renderer.event(event)
        self.assertEqual(self.output.getvalue(), "")

    def test_concurrent_agents_and_duplicate_tool_events_remain_separate(self):
        for identity, name in (("one", "triage"), ("two", "plan")):
            self.renderer.event({"type": "subagent.started", "agentId": identity,
                                 "data": {"agentName": name, "toolCallId": identity}})
            self.renderer.event(message("assistant.message_delta", name + " is working.\n", agentId=identity))
            start = {"id": f"start-{identity}", "type": "tool.execution_start", "agentId": identity,
                     "data": {"toolCallId": "same-local-id", "toolName": "github-get_file"}}
            complete = {"id": f"end-{identity}", "type": "tool.execution_complete", "agentId": identity,
                        "data": {"toolCallId": "same-local-id", "success": True}}
            for event in (start, start, complete, complete):
                self.renderer.event(event)
        self.assertIn("[triage] triage is working.", self.output.getvalue())
        self.assertIn("[plan] plan is working.", self.output.getvalue())
        self.assertEqual(self.renderer.tool_count, 2)
        self.assertEqual(self.output.getvalue().count("github-get_file started"), 2)

    def test_display_limits_do_not_raise_or_grow_unbounded(self):
        self.renderer.MAX_ITEMS = 3
        self.renderer.MAX_MESSAGE = 200
        self.renderer.MAX_TEXT = 400
        self.renderer.MAX_LOG = 250
        for index in range(20):
            self.renderer.event(message("assistant.message", "x" * 2000, str(index)))
        self.renderer.finish("completed")
        self.assertLessEqual(len(self.renderer.messages), 3)
        self.assertLessEqual(self.renderer.text_size, 400)
        self.assertLess(len(self.output.getvalue().encode()), 400)
        self.assertTrue(self.renderer.display_truncated)
        self.assertIn("limits were reached", self.renderer.summary("completed"))

    def test_tool_tracking_limit_and_unknown_completion_are_bounded(self):
        self.renderer.MAX_ITEMS = 2
        for index in range(5):
            self.renderer.event({"type": "tool.execution_start", "data": {
                "toolCallId": str(index), "toolName": "github-get_file",
            }})
        self.renderer.event({"type": "tool.execution_complete", "data": {
            "toolCallId": "not-seen", "success": False, "error": "PRIVATE ERROR",
        }})
        self.assertEqual(len(self.renderer.tools), 2)
        self.assertTrue(self.renderer.display_truncated)
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_summary_preserves_markdown_but_neutralizes_html_images_and_secrets(self):
        text = "## Design\n[Issue](https://github.com/example/project/issues/1)\n<script>bad</script>\n![track](https://evil.test/pixel)\nprivate-token\n::error::fake"
        summary = self.renderer.summary("completed", text=text)
        self.assertIn("## Design", summary)
        self.assertIn("[Issue](https://github.com/example/project/issues/1)", summary)
        self.assertNotIn("<script>", summary)
        self.assertNotIn("![track]", summary)
        self.assertNotIn("private-token", summary)
        self.assertNotIn("::error::", summary)
        self.assertIn("does not assert", summary)
        self.assertNotIn("Design", self.renderer.summary("completed", "status", text=text))
        self.assertEqual(self.renderer.summary("completed", "none", text=text), "")
        self.assertNotIn("Design", self.renderer.summary("failed", text=text))

    def test_large_utf8_summary_is_bounded_and_does_not_change_final_text(self):
        text = "\u754c" * (400 * 1024)
        report = self.renderer.summary("completed", text=text)
        self.assertLessEqual(len(report.encode("utf-8")), self.renderer.MAX_SUMMARY)
        self.assertIn("Summary truncated", report)


class StreamIntegrationTests(unittest.TestCase):
    execute = action_tests.TeamMemoryActionTests.execute
    write_envelope = action_tests.TeamMemoryActionTests.write_envelope
    action_outputs = action_tests.TeamMemoryActionTests.action_outputs

    def setUp(self):
        action_tests.TeamMemoryActionTests.setUp(self)
        self.envelope["request_type"] = "task"
        self.environment["OUTPUT_MODE"] = "hybrid"
        self.environment["SUMMARY_MODE"] = "full"
        self.write_envelope()

    def stream(self, events, done=True):
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        if done:
            body += 'event: done\ndata: {"invocation_id":"fixture","session_id":"fixture"}\n\n'
        return action_tests.Response(body.encode("utf-8"), "text/event-stream")

    def test_default_options_stream_once_and_publish_rendered_final(self):
        self.environment.pop("OUTPUT_MODE")
        self.environment.pop("SUMMARY_MODE")
        answer = "## Plan\nCheck the parser.\n"
        self.execute("submit", [self.stream([
            message("assistant.message_delta", answer),
            message("assistant.streaming_delta", answer),
            message("assistant.message", answer),
        ])])
        self.assertEqual(self.output.getvalue().count("Check the parser."), 1)
        self.assertIn("## Plan", (self.directory / "summary.md").read_text())
        self.assertEqual(self.action_outputs()["status"], "completed")
        self.assertEqual(action_tests.pathlib.Path(self.action_outputs()["response-path"]).read_text(), answer)

    def test_parsing_emits_live_output_before_done_or_eof(self):
        output = io.StringIO()
        renderer = action_tests.action.StreamRenderer(stream=output, secrets=())
        stream = self.stream([message("assistant.message_delta", "Already streaming.\n"),
                              message("assistant.message", "Already streaming.\n")])
        original_readline = stream.readline

        def guarded_readline(size=-1):
            line = original_readline(size)
            if line.startswith(b"event: done"):
                self.assertIn("Already streaming.", output.getvalue())
            return line

        stream.readline = guarded_readline
        self.assertEqual(action_tests.action.read_response(stream, renderer), "Already streaming.\n")

    def test_failed_validation_reports_failure_without_publishing_partial_summary(self):
        self.execute_failed([message("assistant.message_delta", "Partial answer.\n")])
        report = (self.directory / "summary.md").read_text()
        self.assertIn("Partial answer.", self.output.getvalue())
        self.assertIn("outcome is unknown", self.output.getvalue())
        self.assertNotIn("Partial answer.", report)
        self.assertIn("outcome unknown", report)
        self.assertFalse((self.directory / "output.txt").exists())

    def execute_failed(self, events):
        with self.assertRaises(SystemExit):
            self.execute("submit", [self.stream(events, done=False)])

    def test_privacy_modes_and_validation_are_independent(self):
        for mode, summary in (("activity", "status"), ("quiet", "none")):
            with self.subTest(mode=mode):
                summary_file = self.directory / "summary.md"
                summary_file.unlink(missing_ok=True)
                self.environment.update(OUTPUT_MODE=mode, SUMMARY_MODE=summary)
                self.execute("submit", [self.stream([message("assistant.message", "PRIVATE ANSWER")])])
                self.assertEqual(self.action_outputs()["status"], "completed")
                self.assertNotIn("PRIVATE ANSWER", self.output.getvalue())
                if summary == "none":
                    self.assertFalse(summary_file.exists())
                else:
                    self.assertNotIn("PRIVATE ANSWER", summary_file.read_text())

    def test_team_memory_summary_is_a_validated_table_not_json(self):
        self.envelope["request_type"] = "team-memory"
        self.write_envelope()
        self.execute("submit", [self.stream([message("assistant.message", json.dumps(self.result))])])
        report = (self.directory / "summary.md").read_text()
        self.assertIn("| wiki_sha | " + self.result["wiki_sha"], report)
        self.assertNotIn('"wiki_sha"', report)
        self.assertIn("Tool-confirmed update", report)
        self.assertEqual(self.action_outputs()["status"], "updated")

    def test_internal_analysis_cannot_be_published_as_a_final_answer(self):
        for phase in ("analysis", "reasoning"):
            with self.subTest(phase=phase):
                event = message("assistant.message", "PRIVATE ANALYSIS")
                event["data"]["phase"] = phase
                with self.assertRaises(SystemExit):
                    self.execute("submit", [self.stream([event])])
                self.assertNotIn("PRIVATE ANALYSIS", self.output.getvalue())
                self.assertNotIn("PRIVATE ANALYSIS", (self.directory / "summary.md").read_text())
                self.assertFalse((self.directory / "output.txt").exists())

    def test_invalid_modes_fail_before_preflight_network_or_token(self):
        for name in ("OUTPUT_MODE", "SUMMARY_MODE"):
            with self.subTest(name=name):
                previous = self.environment[name]
                self.environment[name] = "raw"
                with self.assertRaises(SystemExit):
                    self.execute("preflight", [])
                self.opener.open.assert_not_called()
                self.token.assert_not_called()
                self.environment[name] = previous

    def test_display_io_errors_do_not_change_validated_success(self):
        class UnavailableOutput:
            def write(self, text):
                raise BrokenPipeError("private log details")

            def flush(self):
                raise BrokenPipeError("private log details")

        self.output = UnavailableOutput()
        self.execute("submit", [self.stream([message("assistant.message", "Validated answer")])])
        self.assertEqual(self.action_outputs()["status"], "completed")
        self.assertIn("Validated answer", (self.directory / "summary.md").read_text())

    def test_summary_io_error_preserves_success_outputs(self):
        import builtins
        original_open = builtins.open

        def fail_summary(path, *arguments, **keywords):
            if str(path) == self.environment["GITHUB_STEP_SUMMARY"]:
                raise PermissionError("private path")
            return original_open(path, *arguments, **keywords)

        with patch("builtins.open", side_effect=fail_summary):
            self.execute("submit", [self.stream([message("assistant.message", "Validated answer")])])
        self.assertEqual(self.action_outputs()["status"], "completed")
        self.assertIn("summary unavailable", self.output.getvalue())
        self.assertNotIn("private path", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
