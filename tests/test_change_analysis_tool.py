"""Offline coverage of read-only MCP transport and isolated SDK worker lifetime."""

import asyncio
import json
import pathlib
import sys
import unittest
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from copilot.tools import ToolInvocation
from mcp.types import CallToolResult, TextContent

import change_analysis_tool as bridge
from change_analysis import MAX_FOCUS_BYTES, AnalysisLimits
from telemetry import RunTelemetry, Settings

if __package__:
    from .test_change_analysis import HEAD, REPOSITORY, FakeModel, FakeSource
    from .test_telemetry import Clock, RecordingBackend
else:
    from test_change_analysis import HEAD, REPOSITORY, FakeModel, FakeSource
    from test_telemetry import Clock, RecordingBackend


SECRET = "PRIVATE-DIFF-CANARY"
SERVER = {
    "command": sys.executable,
    "args": ["-m", "issuelens_github_mcp.server"],
    "env": {"GITHUB_MCP_ENABLE_WRITES": "true", "GITHUB_APP_ID": "1"},
}


class WorkerSession:
    def __init__(self, text="{}", *, block=False, delta=None):
        self.text = text
        self.block = block
        self.delta = delta
        self.handlers = []
        self.sent = asyncio.Event()
        self.aborts = 0
        self.disconnects = 0

    def on(self, callback):
        self.handlers.append(callback)
        return lambda: self.handlers.remove(callback)

    async def send_and_wait(self, prompt, *, timeout):
        self.sent.set()
        data = SimpleNamespace(
            api_call_id="same-id-across-workers", model="offline-model",
            input_tokens=8, output_tokens=2,
        )
        for handler in tuple(self.handlers):
            handler(SimpleNamespace(id=str(uuid.uuid4()), type="assistant.usage", data=data))
            if self.delta is not None:
                handler(SimpleNamespace(
                    id=str(uuid.uuid4()), type="assistant.message_delta",
                    data=SimpleNamespace(delta_content=self.delta),
                ))
        if self.block:
            await asyncio.Event().wait()
        return SimpleNamespace(data=SimpleNamespace(content=self.text))

    async def abort(self):
        self.aborts += 1

    async def disconnect(self):
        self.disconnects += 1


class WorkerClient:
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.options = []
        self.stops = 0

    async def create_session(self, **options):
        self.options.append(options)
        return self.sessions.pop(0)

    async def stop(self):
        self.stops += 1
        return []


class ChangeAnalysisToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.addCleanup(self.backend.provider.shutdown)
        self.run = RunTelemetry(self.backend, Settings(), "invocations")
        self.run.admit()
        self.addCleanup(self.run.finish, "completed")
        self.limits = AnalysisLimits()
        self.read = AsyncMock()
        self.client = WorkerClient([])
        self.directories = []

        async def factory(directory):
            self.assertTrue(pathlib.Path(directory).is_dir())
            self.directories.append(directory)
            return self.client, None, "offline-model"

        @asynccontextmanager
        async def reader(server, run):
            self.assertIs(server, SERVER)
            self.assertIs(run, self.run)
            yield self.read

        self.service = bridge.ChangeAnalysisService(
            SERVER, factory, self.run, reader_factory=reader,
        )
        self.addAsyncCleanup(self.service.close)
        self.addCleanup(patch.stopall)
        patch.object(bridge, "_worker_slots", asyncio.Semaphore(2)).start()
        patch.object(bridge, "_analysis_slots", asyncio.Semaphore(1)).start()

    async def test_each_batch_has_fresh_toolless_context_and_usage(self):
        sessions = [WorkerSession(), WorkerSession()]
        client = WorkerClient(sessions)
        for _ in range(2):
            result = await bridge.complete_batch(
                client, None, "offline-model", self.run, "map", SECRET, self.limits,
            )
            self.assertEqual(result, "{}")
        self.assertEqual(len(client.options), 2)
        for options in client.options:
            self.assertEqual(options["available_tools"], [])
            self.assertEqual(options["mcp_servers"], {})
            self.assertEqual(options["tools"], [])
            self.assertEqual(options["custom_agents"], [])
            self.assertFalse(options["enable_session_store"])
            self.assertFalse(options["enable_config_discovery"])
            self.assertFalse(options["enable_host_git_operations"])
            self.assertFalse(options["enable_skills"])
            self.assertEqual(options["system_message"]["mode"], "replace")
            self.assertNotIn("session_id", options)
            decision = options["on_permission_request"](None, None)
            self.assertEqual(decision.kind, "reject")
        self.assertTrue(all(session.disconnects == 1 for session in sessions))
        self.assertTrue(all(not session.handlers for session in sessions))
        self.assertEqual(self.run.usage.calls, 2)
        self.assertEqual(self.run.usage.totals["input_tokens"], 16)
        self.assertIsNone(self.run.first_output)
        self.assertIsNone(self.run.first_final_output)
        self.assertNotIn(SECRET, repr(self.backend.events))
        self.assertEqual(len(self.backend.facts("issuelens.analysis.worker")), 2)

    async def test_worker_output_overflow_aborts_without_waiting_for_idle(self):
        session = WorkerSession(block=True, delta="x" * (self.limits.max_report_bytes + 1))
        client = WorkerClient([session])
        with self.assertRaisesRegex(bridge.AnalysisRuntimeError, "worker_output_too_large"):
            await asyncio.wait_for(bridge.complete_batch(
                client, None, None, self.run, "map", "prompt", self.limits,
            ), timeout=2)
        self.assertEqual((session.aborts, session.disconnects), (1, 1))
        self.assertEqual(self.run.analysis_workers, {})

    async def test_final_only_output_is_bounded_and_cancellation_closes_worker(self):
        session = WorkerSession("x" * (self.limits.max_report_bytes + 1))
        with self.assertRaisesRegex(bridge.AnalysisRuntimeError, "worker_output_too_large"):
            await bridge.complete_batch(
                WorkerClient([session]), None, None, self.run, "map", "prompt", self.limits,
            )
        waiting = WorkerSession(block=True)
        task = asyncio.create_task(bridge.complete_batch(
            WorkerClient([waiting]), None, None, self.run, "map", "prompt", self.limits,
        ))
        await waiting.sent.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((waiting.aborts, waiting.disconnects), (1, 1))

    async def test_worker_input_is_bounded_before_session_creation(self):
        client = WorkerClient([])
        for size in (
            self.limits.max_prompt_bytes + 1,
            self.limits.max_prompt_bytes - bridge.MODEL_CONTEXT_RESERVE_BYTES + 1,
        ):
            with self.subTest(size=size):
                with self.assertRaisesRegex(bridge.AnalysisRuntimeError, "worker_input_too_large"):
                    await bridge.complete_batch(
                        client, None, None, self.run, "map", "x" * size, self.limits,
                    )
        self.assertLessEqual(
            len(bridge.WORKER_SYSTEM_PROMPT.encode("utf-8")),
            bridge.MODEL_CONTEXT_RESERVE_BYTES,
        )
        self.assertEqual(client.options, [])

    async def test_failed_runtime_stop_uses_sdk_force_cleanup_without_private_errors(self):
        for failure in (RuntimeError(SECRET), [SECRET]):
            with self.subTest(failure=type(failure).__name__):
                client = SimpleNamespace(
                    stop=AsyncMock(
                        side_effect=failure if isinstance(failure, Exception) else None,
                        return_value=failure if isinstance(failure, list) else None,
                    ),
                    force_stop=AsyncMock(),
                )
                with self.assertLogs("change_analysis_tool", level="WARNING") as logs:
                    self.assertFalse(await bridge.stop_analysis_runtime(client))
                client.force_stop.assert_awaited_once()
                self.assertNotIn(SECRET, repr(logs.output))

    async def test_invalid_arguments_never_start_mcp_or_inference(self):
        cases = [
            None, [], {}, {"repository": "owner/repo"},
            {"repository": "owner/repo", "pull_number": True},
            {"repository": "owner/repo", "pull_number": 1, "head_sha": "a" * 40},
            {"repository": "owner/repo", "commit_sha": "main"},
            {"repository": "owner/repo", "commit_sha": "a" * 40, "max_tokens": 100000},
            {"repository": "owner/repo", "pull_number": 1, "focus": "\ud800"},
        ]
        for arguments in cases:
            with self.subTest(arguments=repr(arguments)):
                result = await self.service.tool().handler(ToolInvocation(arguments=arguments))
                self.assertEqual(result.result_type, "failure")
                self.assertEqual(result.error, "invalid_arguments")
        self.assertEqual(self.directories, [])
        self.assertEqual(self.service.admitted, 0)

    async def test_oversized_escaped_focus_is_rejected_before_runtime_start(self):
        cases = (
            "x" * (MAX_FOCUS_BYTES - 1),
            "\\" * ((MAX_FOCUS_BYTES - 2) // 2 + 1),
            '"' * ((MAX_FOCUS_BYTES - 2) // 2 + 1),
            "\n" * ((MAX_FOCUS_BYTES - 2) // 2 + 1),
            "\u00e9" * ((MAX_FOCUS_BYTES - 2) // 6 + 1),
            "\U0001f600" * ((MAX_FOCUS_BYTES - 2) // 12 + 1),
        )
        analyze = AsyncMock(return_value={"status": "complete"})
        with patch.object(bridge, "analyze_change", new=analyze):
            for focus in cases:
                with self.subTest(focus=repr(focus[:12])):
                    response = await self.service.tool().handler(ToolInvocation(arguments={
                        "repository": "owner/repo", "commit_sha": "a" * 40, "focus": focus,
                    }))
                    self.assertEqual(response.error, "invalid_arguments")
                    self.assertEqual(response.result_type, "failure")
                    self.assertIn(str(MAX_FOCUS_BYTES), response.text_result_for_llm)
        analyze.assert_not_called()
        self.assertEqual(self.directories, [])
        self.assertEqual(self.service.admitted, 0)
        self.read.assert_not_called()

    def test_focus_schema_and_boundary_values_match_controller(self):
        schema = self.service.tool().parameters["properties"]["focus"]
        self.assertEqual(schema["maxLength"], MAX_FOCUS_BYTES - 2)
        self.assertIn(str(MAX_FOCUS_BYTES), schema["description"])
        for focus in (
            "", "x" * (MAX_FOCUS_BYTES - 2), "\\" * ((MAX_FOCUS_BYTES - 2) // 2),
            "\u00e9" * ((MAX_FOCUS_BYTES - 2) // 6),
            "\U0001f600" * ((MAX_FOCUS_BYTES - 2) // 12),
        ):
            with self.subTest(focus=repr(focus[:12])):
                result = bridge._arguments({
                    "repository": "owner/repo", "commit_sha": "a" * 40, "focus": focus,
                })
                self.assertEqual(result["focus"], focus)

    async def test_service_stops_runtime_removes_scratch_and_bounds_invocations(self):
        result = {
            "status": "complete", "repository": "owner/repo",
            "summary": SECRET, "coverage": {"files_reviewed": 2},
            "counters": {"model_calls": 3},
        }
        with patch.object(bridge, "analyze_change", new=AsyncMock(return_value=result)) as analyze:
            for _ in range(2):
                response = await self.service.tool().handler(ToolInvocation(arguments={
                    "repository": "owner/repo", "commit_sha": "a" * 40,
                }))
                self.assertEqual(json.loads(response.text_result_for_llm), result)
            denied = await self.service.tool().handler(ToolInvocation(arguments={
                "repository": "owner/repo", "pull_number": 1,
            }))
        self.assertEqual(denied.error, "analysis_limit")
        self.assertEqual(analyze.await_count, 2)
        self.assertEqual(self.client.stops, 2)
        self.assertTrue(all(not pathlib.Path(path).exists() for path in self.directories))
        self.assertNotIn(SECRET, repr(self.backend.events))

    async def test_service_cleanup_cancels_inflight_analysis(self):
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(bridge, "analyze_change", new=blocked):
            task = asyncio.create_task(self.service.tool().handler(ToolInvocation(arguments={
                "repository": "owner/repo", "commit_sha": "a" * 40,
            })))
            await entered.wait()
            await self.service.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.client.stops, 1)
        self.assertTrue(all(not pathlib.Path(path).exists() for path in self.directories))
        self.assertEqual(self.service.tasks, set())

    async def test_controller_budget_excludes_queue_runtime_and_mcp_setup(self):
        for reader_seconds, expected_budget in ((30, 40), (70, None)):
            with self.subTest(reader_seconds=reader_seconds):
                clock = Clock()
                factory = self.service.client_factory

                @asynccontextmanager
                async def slot():
                    clock.value += 10
                    yield

                async def start_runtime(directory):
                    clock.value += 40
                    return await factory(directory)

                @asynccontextmanager
                async def reader(server, run):
                    clock.value += reader_seconds
                    yield self.read

                report = {"status": "complete", "repository": "owner/repo"}
                analyze = AsyncMock(return_value=report)
                with (
                    patch.object(bridge, "time", SimpleNamespace(monotonic=clock)),
                    patch.object(bridge, "_analysis_slots", slot()),
                    patch.object(bridge, "analyze_change", new=analyze),
                ):
                    service = bridge.ChangeAnalysisService(
                        SERVER, start_runtime, self.run, reader_factory=reader,
                        limits=AnalysisLimits(max_seconds=120),
                    )
                    self.addAsyncCleanup(service.close)
                    response = await service.tool().handler(ToolInvocation(arguments={
                        "repository": "owner/repo", "commit_sha": HEAD,
                    }))
                if expected_budget is None:
                    self.assertEqual(response.error, "analysis_failed")
                    analyze.assert_not_awaited()
                else:
                    self.assertEqual(response.result_type, "success")
                    analyze.assert_awaited_once()
                    self.assertEqual(analyze.call_args.kwargs["limits"].max_seconds, expected_budget)
        self.assertEqual(self.client.stops, 2)
        self.assertTrue(all(not pathlib.Path(path).exists() for path in self.directories))

    async def test_controller_deadline_preserves_partial_results_after_setup(self):
        source = FakeSource({"reviewed.py": "+reviewed\n", "waiting.py": "+waiting\n"})
        model = FakeModel()
        cancelled = asyncio.Event()
        reader_closed = asyncio.Event()
        factory = self.service.client_factory

        class MappingSession(WorkerSession):
            async def send_and_wait(self, prompt, *, timeout):
                await super().send_and_wait(prompt, timeout=timeout)
                report = await model(json.loads(prompt)["phase"], prompt)
                return SimpleNamespace(data=SimpleNamespace(content=report))

        session = MappingSession()
        self.client.sessions = [session]

        async def start_runtime(directory):
            await asyncio.sleep(0.05)
            return await factory(directory)

        async def read(name, arguments):
            if name == "read_diff_chunk" and arguments["path"] == "waiting.py":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await source(name, arguments)

        @asynccontextmanager
        async def reader(server, run):
            await asyncio.sleep(0.05)
            try:
                yield read
            finally:
                await asyncio.sleep(0.03)
                reader_closed.set()

        service = bridge.ChangeAnalysisService(
            SERVER, start_runtime, self.run, reader_factory=reader,
            limits=AnalysisLimits(max_seconds=0.4, concurrency=1),
        )
        self.addAsyncCleanup(service.close)
        response = await asyncio.wait_for(service.tool().handler(ToolInvocation(arguments={
            "repository": REPOSITORY, "commit_sha": HEAD,
        })), timeout=2)
        self.assertEqual(response.result_type, "success", response.text_result_for_llm)
        result = json.loads(response.text_result_for_llm)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["files_reviewed"], 1)
        self.assertEqual(result["coverage"]["chunks_reviewed"], 1)
        self.assertIn("deadline_exceeded", result["coverage"]["unresolved_by_code"])
        self.assertEqual(result["findings"][0]["citations"][0]["path"], "reviewed.py")
        self.assertTrue(cancelled.is_set())
        self.assertTrue(reader_closed.is_set())
        self.assertEqual((session.aborts, session.disconnects), (0, 1))
        self.assertEqual(self.client.stops, 1)
        self.assertTrue(all(not pathlib.Path(path).exists() for path in self.directories))
        fact, = self.backend.facts("issuelens.analysis.completed")
        self.assertEqual(fact["status"], "partial")

    async def test_host_admission_precedes_runtime_start_and_queue_uses_deadline(self):
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        other_factory = AsyncMock(side_effect=AssertionError("Queued runtime must not start"))
        other = bridge.ChangeAnalysisService(
            SERVER, other_factory, self.run,
            limits=AnalysisLimits(max_seconds=0.03),
        )
        self.addAsyncCleanup(other.close)
        arguments = {"repository": "owner/repo", "commit_sha": "a" * 40}
        with patch.object(bridge, "analyze_change", new=blocked):
            first = asyncio.create_task(self.service.tool().handler(ToolInvocation(arguments=arguments)))
            await entered.wait()
            response = await other.tool().handler(ToolInvocation(arguments=arguments))
            self.assertEqual(response.result_type, "failure")
            other_factory.assert_not_called()
            await self.service.close()
            with self.assertRaises(asyncio.CancelledError):
                await first
        self.assertEqual(self.client.stops, 1)

    async def test_runtime_failure_is_reported_without_exception_content(self):
        with patch.object(bridge, "analyze_change", new=AsyncMock(side_effect=RuntimeError(SECRET))):
            response = await self.service.tool().handler(ToolInvocation(arguments={
                "repository": "owner/repo", "commit_sha": "a" * 40,
            }))
        self.assertEqual(response.result_type, "failure")
        self.assertEqual(response.error, "analysis_failed")
        self.assertNotIn(SECRET, response.text_result_for_llm)
        self.assertNotIn(SECRET, repr(self.backend.events))
        self.assertEqual(self.client.stops, 1)

    async def test_mcp_bridge_is_read_only_and_rejects_non_json_or_large_results(self):
        class MCPClient:
            response = CallToolResult(
                is_error=False, content=[TextContent(type="text", text='{"repository":"owner/repo"}')],
            )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, *, arguments):
                return self.response

        client = MCPClient()
        with (
            patch("mcp.Client", return_value=client),
            patch("mcp.client.stdio.stdio_client", return_value=object()) as transport,
        ):
            async with bridge.source_reader(SERVER, self.run) as read:
                value = await read("list_change_files", {"repository": "owner/repo"})
                self.assertEqual(value["repository"], "owner/repo")
                with self.assertRaisesRegex(bridge.AnalysisRuntimeError, "unsupported_read"):
                    await read("write_wiki_pages", {"repository": "owner/repo"})
                client.response.content[0].text = "x" * (bridge.MAX_MCP_RESULT_BYTES + 1)
                with self.assertRaisesRegex(bridge.AnalysisRuntimeError, "source_result_too_large"):
                    await read("read_diff_chunk", {"repository": "owner/repo"})
                client.response.is_error = True
                client.response.content[0].text = SECRET
                with self.assertRaisesRegex(bridge.AnalysisRuntimeError, "source_read_failed"):
                    await read("read_file_range", {"repository": "owner/repo"})
        parameters = transport.call_args.args[0]
        self.assertEqual(parameters.env["GITHUB_MCP_ENABLE_WRITES"], "false")
        self.assertEqual(self.run.analysis_counts["analysis_reads"], 3)
        self.assertEqual(self.run.analysis_counts["analysis_reads_failed"], 2)
        self.assertNotIn(SECRET, repr(self.backend.events))


if __name__ == "__main__":
    unittest.main()
