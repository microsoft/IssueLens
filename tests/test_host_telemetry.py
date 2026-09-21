"""Offline integration coverage for both host protocols and run accounting."""

import asyncio
import base64
import importlib.util
import json
import logging
import os
import traceback
from collections import deque
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from azure.ai.agentserver.responses import (
    CreateResponse,
    PlatformContext,
    ResponseContext,
    ResponseEventStream,
)
from azure.ai.agentserver.responses.models import (
    ItemMessage,
    MessageContentInputFileContent,
    MessageContentInputImageContent,
    MessageContentInputTextContent,
)
from azure.ai.agentserver.responses.models.runtime import ResponseModeFlags
from azure.ai.agentserver.responses.streaming._sse import encode_sse_event
from copilot.generated.session_events import SessionEvent, SessionEventType
from opentelemetry.trace import StatusCode
from starlette.requests import Request
from starlette.responses import StreamingResponse

from telemetry import RunTelemetry, Settings

if __package__:
    from .test_telemetry import Clock, RecordingBackend, event
else:
    from test_telemetry import Clock, RecordingBackend, event


ROOT = Path(__file__).resolve().parents[1]
HOST_MODULE = "_issuelens_offline_telemetry_host"
PROMPT_SECRET = "PROMPT-CANARY-private-issue"
ANSWER_SECRET = "ANSWER-CANARY-private-analysis"
ERROR_SECRET = "ERROR-CANARY-credential-query"
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens",
)


class InvocationHost:
    def invoke_handler(self, handler):
        return handler


class ResponsesHost:
    def response_handler(self, handler):
        return handler


def sdk_event(kind, data=None, *, actor=None, identifier=None):
    value = event(kind, data, actor=actor, identifier=identifier)
    value["timestamp"] = "2026-09-20T00:00:00Z"
    return SessionEvent.from_dict(value)


def usage(input_tokens=11, output_tokens=3, *, actor=None, call_id="call1"):
    return sdk_event("assistant.usage", {
        "model": "offline-model", "apiCallId": call_id,
        "inputTokens": input_tokens, "outputTokens": output_tokens,
        "cacheReadTokens": 2, "duration": 100, "timeToFirstTokenMs": 20,
    }, actor=actor)


def answer(text="Offline answer.", *, idle=True):
    events = [
        sdk_event("assistant.message_start", {"messageId": "message1", "phase": "final"}),
        sdk_event("assistant.message_delta", {"messageId": "message1", "deltaContent": text}),
    ]
    if idle:
        events.append(sdk_event("session.idle"))
    return events


def session_error():
    return sdk_event("session.error", {
        "errorType": "provider", "message": ERROR_SECRET, "statusCode": 500,
        "stack": ERROR_SECRET, "url": f"https://example.invalid/?sig={ERROR_SECRET}",
    })


def tool_start(identifier="read1", *, repository="Org/Repo", number=42, actor=None):
    return sdk_event("tool.execution_start", {
        "toolCallId": identifier, "toolName": "github-get_issue",
        "arguments": {"repository": repository, "issue_number": number},
    }, actor=actor)


def tool_complete(identifier="read1", *, number=42, actor=None):
    return sdk_event("tool.execution_complete", {
        "toolCallId": identifier, "success": True,
        "result": {"content": ANSWER_SECRET, "structuredContent": {"number": number}},
    }, actor=actor)


def delegated_turn():
    return [
        usage(),
        sdk_event("tool.execution_start", {
            "toolCallId": "delegation1", "toolName": "task",
            "arguments": {"agent_type": "triage", "prompt": PROMPT_SECRET},
        }),
        sdk_event("subagent.started", {
            "toolCallId": "delegation1", "agentName": "triage",
            "agentDisplayName": "Triage", "agentDescription": "Offline worker",
        }, actor="worker1"),
        usage(5, 2, actor="worker1", call_id="worker-call"),
        tool_start(actor="worker1"),
        tool_complete(actor="worker1"),
        sdk_event("subagent.completed", {
            "toolCallId": "delegation1", "agentName": "triage",
            "agentDisplayName": "Triage", "totalTokens": 99999,
        }, actor="worker1"),
        sdk_event("tool.execution_complete", {
            "toolCallId": "delegation1", "success": True,
            "result": {"content": ANSWER_SECRET},
        }),
        *answer(),
    ]


def sse_frames(chunks):
    text = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
    frames = []
    for frame in text.split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        frames.append((lines.get("event", "message"), json.loads(lines["data"])))
    return frames


class FakeSession:
    def __init__(self, session_id, turns, clock):
        self.session_id = session_id
        self.turns = deque(turns)
        self.clock = clock
        self.handlers = []
        self.sent = asyncio.Event()
        self.send_calls = []
        self.unsubscribe_count = 0
        self.disconnect_count = 0
        self.send_error = None
        self.unsubscribe_error = None
        self.disconnect_error = None

    def on(self, handler):
        self.handlers.append(handler)

        def unsubscribe():
            self.unsubscribe_count += 1
            if self.unsubscribe_error is not None:
                raise self.unsubscribe_error
            self.handlers.remove(handler)

        return unsubscribe

    def emit(self, item):
        self.clock.value += 0.25
        for handler in tuple(self.handlers):
            handler(item)

    async def send(self, prompt, *, attachments=None):
        self.send_calls.append((prompt, attachments))
        self.sent.set()
        if self.send_error is not None:
            raise self.send_error
        for item in self.turns.popleft() if self.turns else []:
            self.emit(item)

    async def disconnect(self):
        self.disconnect_count += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error


class FakeCopilotClient:
    def __init__(self):
        self.pending = deque()
        self.sessions = {}
        self.create_calls = []
        self.resume_calls = []
        self.start_count = 0
        self.create_error = None
        self.resume_error = None

    async def start(self):
        self.start_count += 1

    async def create_session(self, **options):
        self.create_calls.append(options)
        if self.create_error is not None:
            raise self.create_error
        session = self.pending.popleft()
        self.sessions[session.session_id] = session
        return session

    async def resume_session(self, session_id, **options):
        self.resume_calls.append((session_id, options))
        if self.resume_error is not None:
            raise self.resume_error
        return self.sessions[session_id]


class HostTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.network_guards = [
            self.stack.enter_context(patch(
                target, side_effect=AssertionError(f"Offline test attempted {target}"),
            ))
            for target in (
                "socket.create_connection", "socket.socket.connect",
                "httpx.Client.send", "httpx.AsyncClient.send",
            )
        ]
        httpx_logger = logging.getLogger("httpx")
        self.addCleanup(httpx_logger.setLevel, httpx_logger.level)
        spec = importlib.util.spec_from_file_location(HOST_MODULE, ROOT / "main.py")
        self.host = importlib.util.module_from_spec(spec)
        # Real host construction configures global exporters and can query IMDS.
        with (
            patch("azure.ai.agentserver.invocations.InvocationAgentServerHost", InvocationHost),
            patch("azure.ai.agentserver.responses.ResponsesAgentServerHost", ResponsesHost),
            patch("dotenv.load_dotenv"),
            patch("logging.basicConfig"),
            patch("telemetry_export.OpenTelemetryBackend", return_value=None),
            self.assertLogs(HOST_MODULE, level="WARNING") as startup_logs,
        ):
            spec.loader.exec_module(self.host)
        self.startup_logs = startup_logs.output
        self.initial_settings = self.host._telemetry_settings
        self.clock = Clock()
        self.backend = RecordingBackend()
        self.addCleanup(self.backend.provider.shutdown)
        self.host._telemetry_settings = Settings(release="host-integration")
        self.host._telemetry_backend = self.backend
        self.runs = []
        self.stack.enter_context(patch.object(self.host, "RunTelemetry", self.new_run))
        self.client = FakeCopilotClient()
        self.host._client = self.client
        self.copilot_constructor = self.stack.enter_context(patch.object(
            self.host, "CopilotClient", side_effect=AssertionError("Live Copilot CLI startup"),
        ))
        self.github_client = Mock(spec=[])
        self.github_factory = self.stack.enter_context(patch.object(
            self.host, "_new_host_github_client", return_value=self.github_client,
        ))
        self.github_mcp = self.stack.enter_context(patch.object(
            self.host, "_github_mcp_server",
            return_value={"type": "stdio", "command": "offline-only", "args": [], "env": {}},
        ))
        self.config_tool = Mock(name="offline-config-tool")
        self.config_factory = self.stack.enter_context(patch.object(
            self.host, "create_issuelens_config_tool", return_value=self.config_tool,
        ))
        self.images = self.stack.enter_context(patch.object(
            self.host, "issue_image_attachments", autospec=True, return_value=[],
        ))
        self.toolbox = self.stack.enter_context(patch.object(
            self.host, "_toolbox_mcp_server",
            return_value={"type": "http", "url": "https://toolbox.invalid", "tools": []},
        ))
        self.stack.enter_context(patch.object(
            self.host, "_toolbox_bearer", side_effect=AssertionError("Live toolbox authentication"),
        ))
        self.addCleanup(self.assert_offline)

    def assert_offline(self):
        for guard in self.network_guards:
            guard.assert_not_called()
        self.assertEqual(self.github_client.mock_calls, [])

    def new_run(self, backend, settings, protocol, **kwargs):
        run = RunTelemetry(
            backend, settings, protocol, clock=self.clock, wall_clock=self.clock.wall, **kwargs,
        )
        self.runs.append(run)
        return run

    def session(self, events=None, *, session_id="session1", turns=None):
        session = FakeSession(
            session_id, turns if turns is not None else [events or []], self.clock,
        )
        self.client.pending.append(session)
        return session

    def request(self, payload, *, invocation_id="invocation1"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request({
            "type": "http", "method": "POST", "path": "/invocations",
            "headers": [(b"content-type", b"application/json")],
            "state": {"invocation_id": invocation_id},
        }, receive)

    async def invocation(self, payload=None, *, invocation_id="invocation1"):
        response = await self.host.handle_invoke(self.request(
            {"input": PROMPT_SECRET} if payload is None else payload,
            invocation_id=invocation_id,
        ))
        self.assertIsInstance(response, StreamingResponse)
        self.addAsyncCleanup(self.close_stream, response.body_iterator)
        return response

    def chat_stream(
        self, prompt=PROMPT_SECRET, *, conversation="conversation1",
        response_id="response1", content=None, cancellation=None,
    ):
        if content is None:
            content = [MessageContentInputTextContent(text=prompt)] if prompt else []
        items = [ItemMessage(role="user", content=content)] if content else []
        request = CreateResponse(input=items, stream=True, conversation=conversation)
        context = ResponseContext(
            response_id=response_id, request=request, input_items=items,
            conversation_id=conversation,
            mode_flags=ResponseModeFlags(stream=True, store=False, background=False),
            platform_context=PlatformContext(user_id_key="offline-user", call_id="offline-call"),
        )
        stream = self.host.handle_chat(
            request, context, cancellation if cancellation is not None else asyncio.Event(),
        )
        self.addAsyncCleanup(self.close_stream, stream)
        return stream

    async def close_stream(self, stream):
        await stream.aclose()

    async def collect(self, stream):
        return [item async for item in stream]

    async def escaped_error(self, operation):
        try:
            await operation
        except RuntimeError as exc:
            return exc, "".join(traceback.format_exception(exc))
        self.fail("Expected a RuntimeError to escape the host")

    async def chat(self, **kwargs):
        return await self.collect(self.chat_stream(**kwargs))

    async def complete_protocol(self, protocol):
        if protocol == "invocations":
            response = await self.invocation()
            return await self.collect(response.body_iterator)
        return await self.chat()

    def start_consumer(self, stream):
        task = asyncio.create_task(self.collect(stream))

        async def stop():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    def summary(self, **identifiers):
        facts = [
            attributes for name, attributes in self.backend.events
            if name in {"issuelens.run.completed", "issuelens.request.rejected"}
            and all(attributes.get(key) == value for key, value in identifiers.items())
        ]
        self.assertEqual(len(facts), 1, facts)
        return facts[0]

    def assert_no_tokens(self, summary):
        self.assertEqual(summary["usage_calls"], 0)
        for name in TOKEN_FIELDS:
            self.assertNotIn(name, summary)

    def assert_no_canaries(self, value):
        for sentinel in (PROMPT_SECRET, ANSWER_SECRET, ERROR_SECRET):
            self.assertNotIn(sentinel, value)

    def assert_content_free_telemetry(self):
        exported = json.dumps(self.backend.events) + json.dumps(self.backend.metrics) + str([
            (span.name, dict(span.attributes), span.status.description,
             [(item.name, dict(item.attributes)) for item in span.events])
            for span in self.backend.exporter.get_finished_spans()
        ])
        self.assert_no_canaries(exported)

    def assert_sanitized_error(self, caught, formatted, original, message):
        self.assertIsNot(caught, original)
        self.assertEqual(str(caught), message)
        self.assertIsNone(caught.__cause__)
        self.assertTrue(caught.__suppress_context__)
        self.assertIn("Traceback (most recent call last):", formatted)
        self.assertIn(f"RuntimeError: {message}", formatted)
        self.assert_no_canaries(formatted)
        self.assert_content_free_telemetry()

    def active_runs(self):
        return sum(
            value for name, value, _ in self.backend.metrics
            if name == "issuelens.run.active"
        )

    def assert_closed(self, session, *, count=1):
        self.assertEqual(session.unsubscribe_count, count)
        self.assertEqual(session.disconnect_count, count)
        self.assertEqual(session.handlers, [])

    def assert_response(self, events, text, *, response_id="response1"):
        frames = sse_frames([encode_sse_event(item) for item in events])
        self.assertEqual([name for name, _ in frames], [
            "response.created", "response.in_progress",
            "response.output_item.added", "response.content_part.added",
            "response.output_text.delta", "response.output_text.done",
            "response.content_part.done", "response.output_item.done", "response.completed",
        ])
        self.assertEqual([item["sequence_number"] for _, item in frames], list(range(9)))
        self.assertTrue(all(name == item["type"] for name, item in frames))
        response = frames[-1][1]["response"]
        self.assertEqual(response["id"], response_id)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["output"][0]["role"], "assistant")
        self.assertEqual(response["output"][0]["content"][0]["text"], text)
        return response

    def test_import_is_offline_and_retains_real_response_stream(self):
        self.assertIsInstance(self.host.app, InvocationHost)
        self.assertIsInstance(self.host.app, ResponsesHost)
        self.assertIs(self.host.ResponseEventStream, ResponseEventStream)
        self.assertEqual(self.initial_settings, Settings())
        self.assertTrue(any("Application Insights is not configured" in entry for entry in self.startup_logs))
        self.assertEqual(self.host._NOTIFICATION_TOOLS, [])
        self.copilot_constructor.assert_not_called()
        self.assertEqual(self.backend.events, [])

    async def test_analysis_runtime_uses_empty_mode_and_ephemeral_directory(self):
        worker = Mock()
        worker.start = AsyncMock()
        worker.stop = AsyncMock(return_value=[])
        with (
            patch.object(self.host, "_byok_provider", return_value=(None, "offline-model")),
            patch.dict(os.environ, {"GITHUB_TOKEN": "model-token"}),
            patch.object(self.host, "CopilotClient", return_value=worker) as factory,
        ):
            result = await self.host._new_analysis_client("private-worker-directory")
        self.assertEqual(result, (worker, None, "offline-model"))
        self.assertEqual(factory.call_args.kwargs["mode"], "empty")
        self.assertEqual(factory.call_args.kwargs["base_directory"], "private-worker-directory")
        self.assertFalse(factory.call_args.kwargs["use_logged_in_user"])
        worker.start.assert_awaited_once()
        self.assertIs(self.host._client, self.client)

    async def test_invocation_heartbeats_do_not_count_as_assistant_output(self):
        session = self.session([])
        with patch.object(self.host, "_HEARTBEAT_SECONDS", 0.002):
            response = await self.invocation()
            consumer = self.start_consumer(response.body_iterator)
            await session.sent.wait()
            await asyncio.sleep(0.02)
            self.assertIsNone(self.runs[0].first_output)
            for item in answer():
                session.emit(item)
            chunks = await consumer
        self.assertTrue(any(chunk.startswith(b": keep-alive") for chunk in chunks))
        self.assertEqual(sse_frames(chunks)[-1][0], "done")
        self.assert_closed(session)

    async def test_analysis_runtime_start_failure_still_stops_the_private_client(self):
        worker = Mock()
        worker.start = AsyncMock(side_effect=RuntimeError("initialization failed"))
        worker.stop = AsyncMock(side_effect=TimeoutError("PRIVATE-CLEANUP-ERROR"))
        worker.force_stop = AsyncMock()
        with (
            patch.object(self.host, "_byok_provider", return_value=(None, "offline-model")),
            patch.dict(os.environ, {"GITHUB_TOKEN": "model-token"}),
            patch.object(self.host, "CopilotClient", return_value=worker),
            self.assertLogs("change_analysis_tool", level="WARNING") as logs,
            self.assertRaisesRegex(RuntimeError, "initialization failed"),
        ):
            await self.host._new_analysis_client("private-worker-directory")
        worker.stop.assert_awaited_once()
        worker.force_stop.assert_awaited_once()
        self.assertNotIn("PRIVATE-CLEANUP-ERROR", repr(logs.output))
        self.assertIs(self.host._client, self.client)

    async def test_responses_liveness_does_not_emit_worker_text_or_advance_ttft(self):
        session = self.session([])
        with patch.object(self.host, "_HEARTBEAT_SECONDS", 0.002):
            consumer = self.start_consumer(self.chat_stream())
            await session.sent.wait()
            await asyncio.sleep(0.02)
            self.assertIsNone(self.runs[0].first_output)
            for item in answer():
                session.emit(item)
            events = await consumer
        frames = sse_frames([encode_sse_event(item) for item in events])
        self.assertGreater(sum(name == "response.in_progress" for name, _ in frames), 1)
        self.assertEqual(frames[-1][0], "response.completed")
        self.assertEqual(frames[-1][1]["response"]["output"][0]["content"][0]["text"],
                         "Offline answer.")
        tools = self.client.create_calls[0]["tools"]
        self.assertEqual(tools[-1].name, "analyze-change")
        self.assertTrue(tools[-1].handler.__self__.closed)
        self.assert_closed(session)

    async def test_invocations_preserve_raw_sdk_sse_and_account_for_delegated_work(self):
        events = delegated_turn()
        session = self.session(events)
        response = await self.invocation({"input": f"  {PROMPT_SECRET}  "})
        frames = sse_frames(await self.collect(response.body_iterator))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(frames[:-1], [
            ("message", item.to_dict()) for item in events
            if item.type != SessionEventType.SESSION_IDLE
        ])
        self.assertEqual(frames[-1], (
            "done", {"invocation_id": "invocation1", "session_id": "session1"},
        ))
        self.assertEqual(session.send_calls, [(PROMPT_SECRET, None)])
        tools = self.client.create_calls[0]["tools"]
        self.assertEqual(tools[0], self.config_tool)
        self.assertEqual(tools[1].name, "analyze-change")
        self.assertTrue(tools[1].handler.__self__.closed)
        summary = self.summary()
        self.assertEqual((summary["input_tokens"], summary["output_tokens"]), (16, 5))
        self.assertEqual((summary["tools_completed"], summary["agents_started"]), (2, 1))
        self.assertEqual(summary["execution_status"], "completed")
        self.assertEqual(summary["transport_status"], "completed")
        self.assertEqual(summary["write_operations_succeeded"], 0)
        self.assertFalse(summary["telemetry_incomplete"])
        self.assert_closed(session)

    async def test_responses_observe_usage_tools_and_agents_before_presentation_filtering(self):
        session = self.session(delegated_turn())
        events = await self.chat()
        self.assert_response(events, "Offline answer.")
        summary = self.summary()
        self.assertEqual((summary["input_tokens"], summary["output_tokens"]), (16, 5))
        self.assertEqual(summary["usage_calls"], 2)
        self.assertEqual(summary["tools_completed"], 2)
        self.assertEqual(summary["agents_started"], 1)
        self.assertEqual(summary["usage_status"], "complete")
        self.assertIn("first_root_output_s", summary)
        self.assertIn("first_final_output_s", summary)
        roles = {row["role"]: row for row in self.backend.facts("issuelens.run.agent")}
        self.assertEqual(roles["issuelens"]["input_tokens"], 11)
        self.assertEqual(roles["triage"]["input_tokens"], 5)
        read, = [
            row for row in self.backend.facts("issuelens.run.target")
            if row["relationship"] == "read"
        ]
        self.assertEqual((read["repository"], read["target_kind"], read["number"]), ("org/repo", "issue", 42))
        self.assertNotIn("assistant.usage", json.dumps([item.as_dict() for item in events]))
        spans = self.backend.exporter.get_finished_spans()
        root, = [span for span in spans if span.name == "invoke_agent issuelens"]
        phases = [span for span in spans if span.name.startswith("issuelens.")]
        self.assertEqual(
            {span.name for span in phases},
            {"issuelens.media_load", "issuelens.session_open", "issuelens.session_send", "issuelens.session_close"},
        )
        self.assertTrue(all(span.parent.span_id == root.context.span_id for span in phases))
        self.assertTrue(all(span.context.trace_id == root.context.trace_id for span in spans))
        self.assertEqual(len([span for span in spans if span.name == "chat"]), 2)
        self.assert_closed(session)

    async def test_invocation_error_keeps_http_success_and_done_but_records_failed_execution(self):
        session = self.session([usage(), session_error()])
        response = await self.invocation()
        frames = sse_frames(await self.collect(response.body_iterator))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(frames[-2][1], {"type": "error", "message": ERROR_SECRET})
        self.assertEqual(frames[-1][0], "done")
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["transport_status"], "completed")
        self.assertEqual(summary["input_tokens"], 11)
        self.assertEqual(len(self.backend.facts("issuelens.run.error")), 1)
        root, = [
            span for span in self.backend.exporter.get_finished_spans()
            if span.name == "invoke_agent issuelens"
        ]
        self.assertEqual(root.status.status_code, StatusCode.ERROR)
        self.assert_closed(session)

    async def test_response_error_keeps_fallback_and_completion_but_records_failed_execution(self):
        session = self.session([usage(), session_error()])
        events = await self.chat()
        self.assert_response(events, "GitHub tool session failed. Start a new turn to retry.")
        self.assertNotIn(ERROR_SECRET, json.dumps([item.as_dict() for item in events]))
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["transport_status"], "completed")
        self.assertEqual(summary["input_tokens"], 11)
        self.assertNotIn("conversation1", self.host._chat_session_ids)
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assert_closed(session)

    async def test_response_send_failure_uses_fallback_without_fabricating_usage(self):
        session = self.session()
        session.send_error = RuntimeError(ERROR_SECRET)
        self.assert_response(
            await self.chat(), "GitHub tool session failed. Start a new turn to retry.",
        )
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["stage"], "session")
        self.assertEqual(summary["usage_status"], "unavailable")
        self.assert_no_tokens(summary)
        self.assert_closed(session)

    async def test_invocation_configuration_failure_finalizes_before_returning_a_stream(self):
        failure = RuntimeError(ERROR_SECRET)
        self.github_mcp.side_effect = failure
        caught, formatted = await self.escaped_error(
            self.host.handle_invoke(self.request({"input": PROMPT_SECRET})),
        )
        self.assert_sanitized_error(
            caught, formatted, failure, "Could not initialize the IssueLens invocation.",
        )
        summary = self.summary()
        self.assertFalse(summary["admitted"])
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["stage"], "setup")
        self.assertEqual(summary["transport_status"], "rejected")
        self.assertEqual(summary["usage_status"], "not_applicable")
        self.assertEqual(self.client.create_calls, [])
        self.assert_no_tokens(summary)

    async def test_invocation_configuration_error_escapes_safely_and_records_setup_failure(self):
        failure = self.host.ConfigurationError(ERROR_SECRET)
        self.github_mcp.side_effect = failure
        caught, formatted = await self.escaped_error(
            self.host.handle_invoke(self.request({"input": PROMPT_SECRET})),
        )
        self.assertIs(type(caught), RuntimeError)
        self.assert_sanitized_error(
            caught, formatted, failure, "Could not initialize the IssueLens invocation.",
        )
        self.assertEqual(str(failure), ERROR_SECRET)
        summary = self.summary()
        self.assertFalse(summary["admitted"])
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["stage"], "setup")
        self.assertEqual(summary["error_type"], "configuration")
        self.assertEqual(summary["transport_status"], "rejected")
        self.assert_no_tokens(summary)
        self.assertEqual(self.client.create_calls, [])
        self.images.assert_not_awaited()

    async def test_response_configuration_failure_completes_without_model_or_tokens(self):
        self.github_factory.side_effect = self.host.ConfigurationError(ERROR_SECRET)
        self.assert_response(await self.chat(), self.host._GITHUB_APP_UNCONFIGURED)
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["error_type"], "configuration")
        self.assertEqual(summary["transport_status"], "completed")
        self.assertFalse(summary["model_request_sent"])
        self.assert_no_tokens(summary)
        self.assertEqual(self.client.create_calls, [])
        self.assertEqual(self.host._active_chat_conversations, set())

    async def test_session_creation_failures_finalize_both_protocols_and_sanitize_escaped_errors(self):
        for protocol, message in (
            ("invocations", "Could not run the IssueLens invocation."),
            ("responses", "Could not initialize the IssueLens chat turn."),
        ):
            with self.subTest(protocol=protocol):
                failure = RuntimeError(ERROR_SECRET)
                self.client.create_error = failure
                caught, formatted = await self.escaped_error(self.complete_protocol(protocol))
                self.assert_sanitized_error(caught, formatted, failure, message)
                summary = self.summary(protocol=protocol)
                self.assertTrue(summary["admitted"])
                self.assertEqual(summary["execution_status"], "failed")
                self.assertEqual(summary["stage"], "setup")
                self.assertEqual(summary["transport_status"], "interrupted")
                self.assertEqual(summary["usage_status"], "not_applicable")
                self.assert_no_tokens(summary)
                self.assertEqual(self.host._active_chat_conversations, set())

    async def test_default_settings_sanitize_and_account_for_setup_failures(self):
        self.host._telemetry_settings = Settings()
        for entry, message in (
            ("configuration", "Could not initialize the IssueLens invocation."),
            ("invocations", "Could not run the IssueLens invocation."),
            ("responses", "Could not initialize the IssueLens chat turn."),
        ):
            with self.subTest(entry=entry):
                failure = (
                    self.host.ConfigurationError(ERROR_SECRET)
                    if entry == "configuration" else RuntimeError(ERROR_SECRET)
                )
                self.github_mcp.side_effect = failure if entry == "configuration" else None
                self.client.create_error = None if entry == "configuration" else failure
                operation = (
                    self.host.handle_invoke(self.request({"input": PROMPT_SECRET}))
                    if entry == "configuration" else self.complete_protocol(entry)
                )
                caught, formatted = await self.escaped_error(operation)
                self.assert_sanitized_error(caught, formatted, failure, message)
        self.assertTrue(all(run.finished for run in self.runs))
        self.assertEqual(len(self.backend.facts("issuelens.request.rejected")), 1)
        self.assertEqual(len(self.backend.facts("issuelens.run.completed")), 2)
        self.assertTrue(self.backend.metrics)
        self.assert_content_free_telemetry()
        self.assertEqual(self.host._active_chat_conversations, set())

    async def test_invocation_invalid_inputs_and_media_are_rejected_without_starting_sessions(self):
        payloads = [
            b"{invalid-json", [], {"input": "  "},
            {"input": PROMPT_SECRET, "attachments": [{"type": "file", "path": "C:\\private.txt"}]},
            {"input": PROMPT_SECRET, "attachments": [
                {"type": "blob", "mimeType": "image/png", "data": "invalid-base64!"},
            ]},
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                identifier = f"invalid{index}"
                response = await self.host.handle_invoke(self.request(payload, invocation_id=identifier))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.body)["error"], "invalid_request")
                summary = self.summary(invocation_id=identifier)
                self.assertFalse(summary["admitted"])
                self.assertEqual(summary["execution_status"], "rejected")
                self.assertEqual(summary["stage"], "validation")
                self.assertEqual(summary["usage_status"], "not_applicable")
                self.assert_no_tokens(summary)
        self.assertEqual(self.client.create_calls, [])
        self.images.assert_not_awaited()

    async def test_response_remote_media_is_rejected_even_though_transport_completes(self):
        for index, content in enumerate((
            MessageContentInputImageContent(detail="auto", image_url="https://example.invalid/private.png"),
            MessageContentInputFileContent(file_url="https://example.invalid/private.txt"),
        )):
            with self.subTest(index=index):
                identifier = f"invalid-response{index}"
                events = await self.chat(content=[content], response_id=identifier)
                final = events[-1].as_dict()
                self.assertEqual(final["type"], "response.completed")
                self.assertTrue(final["response"]["output"][0]["content"][0]["text"].startswith("Unsupported attachment:"))
                summary = self.summary(response_id=identifier)
                self.assertFalse(summary["admitted"])
                self.assertEqual(summary["execution_status"], "rejected")
                self.assertEqual(summary["stage"], "validation")
                self.assertEqual(summary["transport_status"], "completed")
                self.assert_no_tokens(summary)
        self.assertEqual(self.client.create_calls, [])
        self.assertEqual(self.host._active_chat_conversations, set())

    async def test_valid_invocation_attachments_reach_session_without_changing_wire_protocol(self):
        inline = {"type": "blob", "data": base64.b64encode(b"inline").decode(), "mimeType": "image/png"}
        loaded = {"type": "blob", "data": base64.b64encode(b"issue image").decode(), "mimeType": "image/png"}
        self.images.return_value = [loaded]
        session = self.session([usage(), *answer()])
        response = await self.invocation({"input": PROMPT_SECRET, "attachments": [inline]})
        frames = sse_frames(await self.collect(response.body_iterator))
        self.assertEqual(frames[-1][0], "done")
        self.assertEqual(session.send_calls, [(PROMPT_SECRET, [inline, loaded])])
        self.images.assert_awaited_once_with(
            PROMPT_SECRET, self.github_client, maximum_images=9,
            on_issue_read=self.runs[0].host_issue_read,
        )
        self.assertNotIn(inline["data"], json.dumps(self.backend.events))

    async def test_attachment_only_response_uses_model_instead_of_greeting(self):
        encoded = base64.b64encode(b"issue details").decode()
        session = self.session([usage(), *answer()])
        events = await self.chat(content=[
            MessageContentInputFileContent(filename="details.txt", file_data=encoded),
        ])
        self.assert_response(events, "Offline answer.")
        prompt, attachments = session.send_calls[0]
        self.assertEqual(json.loads(prompt.split("\n", 1)[1])["user_input"], self.host._ATTACHMENT_ONLY_PROMPT)
        self.assertEqual(attachments[0]["data"], encoded)
        self.assertTrue(self.summary()["model_request_sent"])
        self.assertEqual(self.summary()["input_tokens"], 11)

    async def test_unavailable_issue_images_degrade_accounting_but_do_not_fail_either_protocol(self):
        self.images.side_effect = self.host.GitHubAppError(ERROR_SECRET)
        for protocol in ("invocations", "responses"):
            with self.subTest(protocol=protocol):
                session = self.session([usage(), *answer()], session_id=f"{protocol}-session")
                await self.complete_protocol(protocol)
                summary = self.summary(protocol=protocol)
                self.assertEqual(summary["execution_status"], "completed")
                self.assertTrue(summary["telemetry_incomplete"])
                self.assertEqual(summary["incomplete_media_unavailable"], 1)
                self.assertEqual(summary["input_tokens"], 11)
                self.assert_closed(session)
        self.assertEqual(self.backend.facts("issuelens.run.target"), [])

    async def test_successful_host_issue_reads_reach_target_telemetry_for_both_protocols(self):
        prompt = "Triage Org/Media#73 and Org/Unread#99."
        callbacks = []

        async def load_images(
            task, github_client, *, maximum_images=5, on_issue_read=None,
        ):
            self.assertEqual(task, prompt)
            self.assertIs(github_client, self.github_client)
            self.assertEqual(maximum_images, 10)
            self.assertIsNotNone(on_issue_read)
            callbacks.append(on_issue_read)
            on_issue_read("Org/Media", 73)
            return []

        self.images.side_effect = load_images
        for protocol in ("invocations", "responses"):
            with self.subTest(protocol=protocol):
                session = self.session([sdk_event("session.idle")], session_id=f"{protocol}-session")
                if protocol == "invocations":
                    response = await self.invocation({"input": prompt})
                    await self.collect(response.body_iterator)
                else:
                    await self.chat(prompt=prompt)
                summary = self.summary(protocol=protocol)
                target, = [
                    row for row in self.backend.facts("issuelens.run.target")
                    if row["run_id"] == summary["run_id"]
                ]
                self.assertEqual(
                    (target["repository"], target["number"], target["relationship"], target["operations"]),
                    ("org/media", 73, "read", 1),
                )
                self.assertEqual(summary["execution_status"], "completed")
                self.assertEqual(summary["tools_started"], 0)
                self.assertEqual(summary["write_operations_succeeded"], 0)
                self.assert_no_tokens(summary)
                self.assert_closed(session)
        before = list(self.backend.events)
        for callback in callbacks:
            callback("Org/TooLate", 100)
        self.assertEqual(self.backend.events, before)

    async def test_two_response_turns_resume_session_with_distinct_non_cumulative_runs(self):
        session = self.session(turns=[
            [usage(13, 3), *answer("First answer.")],
            [usage(7, 2), *answer("Second answer.")],
        ])
        self.assert_response(await self.chat(response_id="response1"), "First answer.")
        self.assert_response(
            await self.chat(response_id="response2"), "Second answer.", response_id="response2",
        )
        first, second = (self.summary(response_id=identifier) for identifier in ("response1", "response2"))
        self.assertEqual(len(self.client.create_calls), 1)
        self.assertEqual(len(self.client.resume_calls), 1)
        self.assertEqual(self.client.resume_calls[0][0], session.session_id)
        self.assertEqual(self.host._chat_session_ids, {"conversation1": session.session_id})
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["run_trace_id"], second["run_trace_id"])
        self.assertTrue(first["run_trace_id"])
        self.assertEqual((first["input_tokens"], second["input_tokens"]), (13, 7))
        self.assertEqual((first["usage_calls"], second["usage_calls"]), (1, 1))
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assert_closed(session, count=2)

    async def test_resume_failure_uses_new_session_and_marks_degradation_without_old_usage(self):
        original = self.session([usage(13), *answer()])
        await self.chat()
        replacement = self.session([usage(2, 1), *answer()], session_id="replacement")
        self.client.resume_error = RuntimeError(ERROR_SECRET)
        await self.chat(response_id="response2")
        summary = self.summary(response_id="response2")
        self.assertEqual(summary["execution_status"], "completed")
        self.assertEqual(summary["input_tokens"], 2)
        self.assertEqual(summary["incomplete_resume_fallback"], 1)
        self.assertEqual(summary["session_id"], replacement.session_id)
        self.assertEqual(self.host._chat_session_ids["conversation1"], replacement.session_id)
        self.assert_closed(original)
        self.assert_closed(replacement)

    async def test_interleaved_conversations_isolate_usage_targets_and_lifecycles(self):
        first = self.session(session_id="session-a")
        second = self.session(session_id="session-b")
        task_a = self.start_consumer(self.chat_stream(conversation="conversation-a", response_id="response-a"))
        await asyncio.wait_for(first.sent.wait(), 2)
        task_b = self.start_consumer(self.chat_stream(conversation="conversation-b", response_id="response-b"))
        await asyncio.wait_for(second.sent.wait(), 2)
        self.assertEqual(self.active_runs(), 2)
        first.emit(usage(10, 1))
        second.emit(usage(100, 9))
        first.emit(tool_start(repository="Org/First", number=1))
        second.emit(tool_start(repository="Org/Second", number=2))
        second.emit(tool_complete(number=2))
        for item in answer("Second conversation."):
            second.emit(item)
        await asyncio.wait_for(task_b, 2)
        self.assertFalse(task_a.done())
        self.assertEqual(self.host._active_chat_conversations, {"conversation-a"})
        self.assertEqual(self.active_runs(), 1)
        first.emit(tool_complete(number=1))
        for item in answer("First conversation."):
            first.emit(item)
        await asyncio.wait_for(task_a, 2)
        a, b = (self.summary(response_id=identifier) for identifier in ("response-a", "response-b"))
        self.assertEqual((a["input_tokens"], b["input_tokens"]), (10, 100))
        self.assertNotEqual(a["run_id"], b["run_id"])
        self.assertNotEqual(a["run_trace_id"], b["run_trace_id"])
        for summary, repository in ((a, "org/first"), (b, "org/second")):
            targets = [
                row for row in self.backend.facts("issuelens.run.target")
                if row["run_id"] == summary["run_id"]
            ]
            self.assertEqual({row["repository"] for row in targets}, {repository})
            self.assertEqual(summary["tools_completed"], 1)
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assertEqual(self.active_runs(), 0)
        self.assert_closed(first)
        self.assert_closed(second)

    async def test_overlapping_same_conversation_is_rejected_without_disturbing_active_turn(self):
        session = self.session()
        active = self.start_consumer(self.chat_stream())
        await asyncio.wait_for(session.sent.wait(), 2)
        events = await self.chat(response_id="overlap")
        self.assertIn("already active", events[-1].as_dict()["response"]["output"][0]["content"][0]["text"])
        rejected = self.summary(response_id="overlap")
        self.assertFalse(rejected["admitted"])
        self.assertEqual(rejected["execution_status"], "rejected")
        self.assertEqual(rejected["error_type"], "concurrent_turn")
        self.assertEqual(rejected["transport_status"], "completed")
        self.assertEqual(session.disconnect_count, 0)
        self.assertEqual(session.unsubscribe_count, 0)
        self.assertEqual(len(session.handlers), 1)
        self.assertEqual(self.client.resume_calls, [])
        self.assertEqual(len(self.client.create_calls), 1)
        self.assertEqual(self.host._active_chat_conversations, {"conversation1"})
        self.assertEqual(self.active_runs(), 1)
        self.assertEqual(self.host._chat_session_ids["conversation1"], session.session_id)
        for item in [usage(), *answer()]:
            session.emit(item)
        await asyncio.wait_for(active, 2)
        self.assertEqual(self.summary(response_id="response1")["execution_status"], "completed")
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assertEqual(self.active_runs(), 0)
        self.assert_closed(session)

    async def test_pre_set_response_cancellation_never_opens_session_or_sends_model_request(self):
        session = self.session()
        cancellation = asyncio.Event()
        cancellation.set()
        stream = self.chat_stream(cancellation=cancellation)
        events = []
        with self.assertRaises(asyncio.CancelledError):
            async for item in stream:
                events.append(item.type)
        await stream.aclose()
        self.assertEqual(events, ["response.created", "response.in_progress"])
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "cancelled")
        self.assertEqual(summary["transport_status"], "interrupted")
        self.assertFalse(summary["admitted"])
        self.assertFalse(summary["model_request_sent"])
        self.assertEqual(summary["usage_status"], "not_applicable")
        self.assert_no_tokens(summary)
        self.assertEqual(session.send_calls, [])
        self.assertEqual(session.unsubscribe_count, 0)
        self.assertEqual(session.disconnect_count, 0)
        self.assertEqual(self.client.create_calls, [])
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assertEqual(self.host._chat_session_ids, {})
        self.assertEqual(self.active_runs(), 0)
        self.github_factory.assert_not_called()
        self.images.assert_not_awaited()

    async def test_response_cancellation_during_awaited_setup_blocks_dispatch_and_cleans_session(self):
        baseline_tasks = asyncio.all_tasks()
        session = self.session()
        cancellation = asyncio.Event()
        setup_started, release_setup = asyncio.Event(), asyncio.Event()
        create_session = self.client.create_session

        async def delayed_create(**options):
            setup_started.set()
            await release_setup.wait()
            return await create_session(**options)

        with patch.object(self.client, "create_session", side_effect=delayed_create):
            stream = self.chat_stream(cancellation=cancellation)
            active = self.start_consumer(stream)
            await asyncio.wait_for(setup_started.wait(), 2)
            self.assertEqual(self.host._active_chat_conversations, {"conversation1"})
            self.assertEqual(self.active_runs(), 1)
            cancellation.set()
            release_setup.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(active, 2)
            await stream.aclose()
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "cancelled")
        self.assertEqual(summary["transport_status"], "interrupted")
        self.assertTrue(summary["admitted"])
        self.assertFalse(summary["model_request_sent"])
        self.assertEqual(summary["usage_status"], "not_applicable")
        self.assert_no_tokens(summary)
        self.assertEqual(session.send_calls, [])
        self.assert_closed(session)
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assertEqual(self.host._chat_session_ids, {})
        self.assertEqual(self.active_runs(), 0)
        self.assertEqual(asyncio.all_tasks() - baseline_tasks, set())

    async def test_response_cancellation_signal_finalizes_once_and_releases_session_guard(self):
        baseline_tasks = asyncio.all_tasks()
        session = self.session()
        cancellation = asyncio.Event()
        stream = self.chat_stream(cancellation=cancellation)
        active = self.start_consumer(stream)
        await asyncio.wait_for(session.sent.wait(), 2)
        cancellation.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(active, 2)
        await stream.aclose()
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "cancelled")
        self.assertEqual(summary["transport_status"], "interrupted")
        self.assertEqual(summary["error_type"], "cancelled")
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assertEqual(self.host._chat_session_ids, {})
        self.assert_closed(session)
        before = list(self.backend.events)
        session.emit(usage(1000, 1000))
        self.assertEqual(self.backend.events, before)
        self.assertEqual(asyncio.all_tasks() - baseline_tasks, set())

    async def test_closing_invocation_generator_cancels_once_and_detaches_observer(self):
        session = self.session([usage(), *answer(idle=False)])
        response = await self.invocation()
        stream = response.body_iterator
        await anext(stream)
        await stream.aclose()
        await stream.aclose()
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "cancelled")
        self.assertEqual(summary["transport_status"], "interrupted")
        self.assertEqual(summary["input_tokens"], 11)
        self.assert_closed(session)
        before = list(self.backend.events)
        session.emit(session_error())
        self.assertEqual(self.backend.events, before)

    async def test_closing_response_generator_cancels_once_and_clears_guard_and_watcher(self):
        baseline_tasks = asyncio.all_tasks()
        session = self.session([usage(), *answer(idle=False)])
        stream = self.chat_stream()
        while (await anext(stream)).type != "response.output_text.delta":
            pass
        await stream.aclose()
        await stream.aclose()
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "cancelled")
        self.assertEqual(summary["transport_status"], "interrupted")
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assertEqual(self.host._chat_session_ids, {})
        self.assert_closed(session)
        self.assertEqual(asyncio.all_tasks() - baseline_tasks, set())

    async def test_closing_response_before_setup_finalizes_without_opening_session(self):
        stream = self.chat_stream()
        self.assertEqual((await anext(stream)).type, "response.created")
        await stream.aclose()
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "cancelled")
        self.assertFalse(summary["admitted"])
        self.assertFalse(summary["model_request_sent"])
        self.assert_no_tokens(summary)
        self.assertEqual(self.client.create_calls, [])

    async def test_closing_invocation_after_done_preserves_known_execution_status(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                terminal = answer() if status == "completed" else [session_error(), sdk_event("session.idle")]
                session = self.session([usage(), *terminal], session_id=f"{status}-session")
                response = await self.invocation(invocation_id=f"{status}-invocation")
                stream = response.body_iterator
                events = []
                async for item in stream:
                    frame, = sse_frames([item])
                    events.append(frame)
                    if frame[0] == "done":
                        break
                self.assertEqual(events[-1][0], "done")
                await stream.aclose()
                await stream.aclose()
                summary = self.summary(invocation_id=f"{status}-invocation")
                self.assertEqual(summary["execution_status"], status)
                self.assertEqual(summary["transport_status"], "completed")
                self.assertNotEqual(summary["error_type"], "cancelled")
                self.assertEqual(summary["input_tokens"], 11)
                self.assert_closed(session)
                self.assertEqual(self.active_runs(), 0)

    async def test_closing_invocation_after_error_preserves_failure_with_interrupted_delivery(self):
        session = self.session([usage(), session_error(), sdk_event("session.idle")])
        response = await self.invocation()
        stream = response.body_iterator
        events = []
        async for item in stream:
            frame, = sse_frames([item])
            events.append(frame)
            if frame[1].get("type") == "error":
                break
        self.assertEqual(events[-1][1]["type"], "error")
        self.assertNotIn("done", [name for name, _ in events])
        await stream.aclose()
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["error_type"], "execution_error")
        self.assertEqual(summary["transport_status"], "interrupted")
        self.assert_closed(session)
        self.assertEqual(self.active_runs(), 0)

    async def test_closing_response_after_text_done_preserves_execution_despite_interrupted_delivery(self):
        baseline_tasks = asyncio.all_tasks()
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                terminal = answer() if status == "completed" else [session_error(), sdk_event("session.idle")]
                session = self.session([usage(), *terminal], session_id=f"{status}-session")
                conversation = f"{status}-conversation"
                stream = self.chat_stream(conversation=conversation, response_id=f"{status}-response")
                events = []
                async for item in stream:
                    events.append(item.type)
                    if item.type == "response.output_text.done":
                        break
                self.assertEqual(events[-1], "response.output_text.done")
                self.assertNotIn("response.completed", events)
                await stream.aclose()
                await stream.aclose()
                summary = self.summary(response_id=f"{status}-response")
                self.assertEqual(summary["execution_status"], status)
                self.assertEqual(summary["transport_status"], "interrupted")
                self.assertNotEqual(summary["error_type"], "cancelled")
                self.assertEqual(summary["input_tokens"], 11)
                self.assertEqual(self.host._active_chat_conversations, set())
                if status == "completed":
                    self.assertEqual(self.host._chat_session_ids[conversation], session.session_id)
                else:
                    self.assertNotIn(conversation, self.host._chat_session_ids)
                self.assert_closed(session)
                self.assertEqual(self.active_runs(), 0)
        self.assertEqual(asyncio.all_tasks() - baseline_tasks, set())

    async def test_completed_response_closed_at_either_completion_boundary_resumes_next_turn(self):
        for index, close_after in enumerate(("response.output_text.done", "response.completed")):
            with self.subTest(close_after=close_after):
                session = self.session(
                    session_id=f"resumable-session{index}",
                    turns=[
                        [usage(), *answer("First answer.")],
                        [usage(4, 1), *answer("Resumed answer.")],
                    ],
                )
                conversation = f"resumable-conversation{index}"
                response_id = f"closed-response{index}"
                creates_before = len(self.client.create_calls)
                stream = self.chat_stream(conversation=conversation, response_id=response_id)
                events = []
                async for item in stream:
                    events.append(item.type)
                    if item.type == close_after:
                        break
                self.assertEqual(events[-1], close_after)
                await stream.aclose()
                summary = self.summary(response_id=response_id)
                self.assertEqual(summary["execution_status"], "completed")
                self.assertEqual(
                    summary["transport_status"],
                    "completed" if close_after == "response.completed" else "interrupted",
                )
                self.assertEqual(self.host._chat_session_ids[conversation], session.session_id)
                self.assert_closed(session)
                resumed_id = f"resumed-response{index}"
                resumes_before = len(self.client.resume_calls)
                self.assert_response(
                    await self.chat(conversation=conversation, response_id=resumed_id),
                    "Resumed answer.", response_id=resumed_id,
                )
                self.assertEqual(len(self.client.create_calls), creates_before + 1)
                self.assertEqual(len(self.client.resume_calls), resumes_before + 1)
                self.assertEqual(self.client.resume_calls[-1][0], session.session_id)
                resumed = self.summary(response_id=resumed_id)
                self.assertEqual(resumed["session_id"], summary["session_id"])
                self.assertNotEqual(resumed["run_id"], summary["run_id"])
                self.assertEqual(resumed["input_tokens"], 4)
                self.assert_closed(session, count=2)
                self.assertEqual(self.host._active_chat_conversations, set())
                self.assertEqual(self.active_runs(), 0)

    async def test_disconnect_failure_marks_incomplete_without_masking_either_protocol_result(self):
        for protocol in ("invocations", "responses"):
            with self.subTest(protocol=protocol):
                session = self.session([usage(), *answer()], session_id=f"{protocol}-session")
                session.disconnect_error = RuntimeError(ERROR_SECRET)
                result = await self.complete_protocol(protocol)
                if protocol == "invocations":
                    self.assertEqual(sse_frames(result)[-1][0], "done")
                else:
                    self.assert_response(result, "Offline answer.")
                summary = self.summary(protocol=protocol)
                self.assertEqual(summary["execution_status"], "completed")
                self.assertTrue(summary["telemetry_incomplete"])
                self.assertEqual(summary["incomplete_cleanup_failed"], 1)
                self.assert_closed(session)
        self.assertEqual(self.host._active_chat_conversations, set())

    async def test_unsubscribe_failure_degrades_both_protocols_and_still_disconnects_without_secret_logs(self):
        for protocol in ("invocations", "responses"):
            with self.subTest(protocol=protocol):
                session = self.session([usage(), *answer()], session_id=f"{protocol}-session")
                session.unsubscribe_error = RuntimeError(ERROR_SECRET)
                with self.assertLogs(self.host.logger, level=logging.WARNING) as logs:
                    result = await self.complete_protocol(protocol)
                if protocol == "invocations":
                    self.assertEqual(sse_frames(result)[-1][0], "done")
                else:
                    self.assert_response(result, "Offline answer.")
                self.assertIn("Copilot observer cleanup failed", "\n".join(logs.output))
                self.assert_no_canaries("\n".join(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                summary = self.summary(protocol=protocol)
                self.assertEqual(summary["execution_status"], "completed")
                self.assertEqual(summary["transport_status"], "completed")
                self.assertTrue(summary["telemetry_incomplete"])
                self.assertEqual(summary["incomplete_cleanup_failed"], 1)
                self.assertEqual(session.unsubscribe_count, 1)
                self.assertEqual(session.disconnect_count, 1)
                before = list(self.backend.events)
                session.emit(usage(1000, 1000))
                self.assertEqual(self.backend.events, before)
                self.assert_content_free_telemetry()
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assertEqual(self.active_runs(), 0)

    async def test_invocation_disconnect_failure_preserves_sanitized_send_failure(self):
        session = self.session()
        original = RuntimeError("Original " + ERROR_SECRET)
        session.send_error = original
        session.disconnect_error = RuntimeError("Cleanup " + ERROR_SECRET)
        response = await self.invocation()
        caught, formatted = await self.escaped_error(self.collect(response.body_iterator))
        self.assert_sanitized_error(
            caught, formatted, original, "Could not run the IssueLens invocation.",
        )
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["transport_status"], "interrupted")
        self.assertEqual(summary["incomplete_cleanup_failed"], 1)
        self.assert_closed(session)

    async def test_default_settings_sanitize_send_failure_despite_disconnect_failure(self):
        self.host._telemetry_settings = Settings()
        session = self.session()
        original = RuntimeError("Original " + ERROR_SECRET)
        session.send_error = original
        session.disconnect_error = RuntimeError("Cleanup " + ERROR_SECRET)
        response = await self.invocation()
        caught, formatted = await self.escaped_error(self.collect(response.body_iterator))
        self.assert_sanitized_error(
            caught, formatted, original, "Could not run the IssueLens invocation.",
        )
        self.assertNotIn("Cleanup " + ERROR_SECRET, formatted)
        self.assert_closed(session)
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "failed")
        self.assertEqual(summary["incomplete_cleanup_failed"], 1)
        self.assertTrue(self.backend.metrics)
        self.assertTrue(self.runs[0].finished)

    async def test_application_logs_and_telemetry_do_not_contain_prompt_answer_or_error_secrets(self):
        with self.assertLogs(level=logging.INFO) as logs:
            self.session([usage(), *answer(ANSWER_SECRET)], session_id="successful")
            await self.chat()
            failed = self.session([session_error()], session_id="failed")
            failed.disconnect_error = RuntimeError(ERROR_SECRET)
            await self.chat(conversation="failed-conversation", response_id="failed-response")
            self.images.side_effect = self.host.GitHubAppError(ERROR_SECRET)
            self.session([session_error()], session_id="invocation")
            response = await self.invocation()
            await self.collect(response.body_iterator)
        self.assert_no_canaries("\n".join(logs.output))
        self.assert_content_free_telemetry()
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    async def test_greeting_is_no_action_with_no_session_or_fabricated_tokens(self):
        events = await self.chat(prompt="")
        self.assert_response(events, self.host._GREETING)
        summary = self.summary()
        self.assertEqual(summary["execution_status"], "completed")
        self.assertEqual(summary["business_outcome"], "no_action")
        self.assertEqual(summary["usage_status"], "not_applicable")
        self.assertFalse(summary["model_request_sent"])
        self.assert_no_tokens(summary)
        self.assertNotIn("first_root_output_s", summary)
        self.assertEqual(self.client.create_calls, [])
        self.assertEqual(self.client.resume_calls, [])
        self.github_factory.assert_not_called()
        self.images.assert_not_awaited()

    async def test_idle_without_usage_reports_unavailable_instead_of_zero_tokens(self):
        for protocol in ("invocations", "responses"):
            with self.subTest(protocol=protocol):
                self.session([sdk_event("session.idle")], session_id=f"{protocol}-session")
                await self.complete_protocol(protocol)
                summary = self.summary(protocol=protocol)
                self.assertEqual(summary["execution_status"], "completed")
                self.assertTrue(summary["model_request_sent"])
                self.assertEqual(summary["usage_status"], "unavailable")
                self.assert_no_tokens(summary)
                self.assertNotIn("first_root_output_s", summary)
        self.assertEqual(self.backend.facts("issuelens.run.model"), [])

    async def test_default_settings_collect_both_protocols_without_changing_results(self):
        self.host._telemetry_settings = Settings()
        invocation_session = self.session([usage(), *answer()], session_id="invocation-session")
        response = await self.invocation()
        self.assertEqual(sse_frames(await self.collect(response.body_iterator))[-1][0], "done")
        chat_session = self.session([usage(), *answer()], session_id="chat-session")
        self.assert_response(await self.chat(), "Offline answer.")
        summaries = self.backend.facts("issuelens.run.completed")
        self.assertEqual({summary["protocol"] for summary in summaries}, {"invocations", "responses"})
        self.assertEqual(len(summaries), 2)
        self.assertTrue(all(summary["usage_status"] == "complete" for summary in summaries))
        self.assertTrue(all("environment" not in summary for summary in summaries))
        self.assertTrue(self.backend.metrics)
        self.assertTrue(self.backend.exporter.get_finished_spans())
        self.assertEqual(self.host._active_chat_conversations, set())
        self.assert_closed(invocation_session)
        self.assert_closed(chat_session)

    async def test_client_start_disables_native_exporter_only_in_child_environment(self):
        self.host._client = None
        self.copilot_constructor.side_effect = None
        self.copilot_constructor.return_value = self.client
        environment = {
            "GITHUB_TOKEN": "offline-model-token",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://exporter.invalid",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=offline-secret",
            "COPILOT_OTEL_ENABLED": "true",
            "COPILOT_OTEL_CAPTURE_CONTENT": "true",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
        }
        with patch.dict(os.environ, environment), patch.object(self.host.os, "makedirs") as mkdir:
            original = dict(os.environ)
            self.assertIs(await self.host._ensure_client(), self.client)
            self.assertIs(await self.host._ensure_client(), self.client)
            self.assertEqual(dict(os.environ), original)
        mkdir.assert_called_once()
        self.copilot_constructor.assert_called_once()
        self.assertEqual(self.client.start_count, 1)
        options = self.copilot_constructor.call_args.kwargs
        child_environment = options["env"]
        self.assertEqual(options["github_token"], "offline-model-token")
        self.assertEqual(child_environment["COPILOT_OTEL_ENABLED"], "false")
        self.assertEqual(child_environment["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")
        self.assertFalse(any(key.startswith("OTEL_EXPORTER_OTLP") for key in child_environment))
        self.assertNotIn("COPILOT_OTEL_CAPTURE_CONTENT", child_environment)


if __name__ == "__main__":
    unittest.main()
