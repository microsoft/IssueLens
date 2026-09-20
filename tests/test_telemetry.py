import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from copilot.generated.session_events import (
    AssistantUsageData, ModelCallFailureSource, SessionEvent, SessionEventType,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from telemetry import RunTelemetry, Settings, copilot_environment, prepare_environment
from telemetry_targets import result_metadata


class RecordingBackend:
    def __init__(self):
        self.events = []
        self.metrics = []
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("telemetry-test")
        self.reject_events = False

    def event(self, name, attributes):
        self.events.append((name, dict(attributes)))
        return not self.reject_events

    def metric(self, name, value, attributes):
        self.metrics.append((name, value, dict(attributes)))

    def span(self, name, attributes, *, parent=None, start_ns=None):
        return self.tracer.start_span(
            name, attributes=attributes, start_time=start_ns,
            context=trace.set_span_in_context(parent) if parent is not None else None,
        )

    def facts(self, name):
        return [attributes for event, attributes in self.events if event == name]


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def wall(self):
        return 1_800_000_000_000_000_000 + int(self.value * 1e9)


def event(kind, data=None, *, actor=None, identifier=None):
    result = {"id": identifier or str(uuid.uuid4()), "type": kind, "data": data or {}}
    if actor is not None:
        result["agentId"] = actor
    return result


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.addCleanup(self.backend.provider.shutdown)
        self.clock = Clock()
        self.run = RunTelemetry(
            self.backend, Settings(release="release1"), "responses",
            identifiers={"conversation_id": "conversation1", "response_id": "response1"},
            clock=self.clock, wall_clock=self.clock.wall,
        )
        self.run.admit()
        self.run.model_sent = True

    def complete(self, **kwargs):
        self.run.finish("completed", transport_status="completed", **kwargs)
        return self.backend.facts("issuelens.run.completed")[-1]

    def usage(self, **kwargs):
        data = {"model": "gpt-test", "inputTokens": 100, "outputTokens": 20, **kwargs}
        return event("assistant.usage", data)

    def tool_start(self, identifier="read1", name="github-get_issue", actor=None, **args):
        self.run.observe(event("tool.execution_start", {
            "toolCallId": identifier, "toolName": name,
            "arguments": {"repository": "Org/Repo", "issue_number": 42, **args},
        }, actor=actor))

    def tool_end(self, identifier="read1", actor=None, success=True, **result):
        self.run.observe(event("tool.execution_complete", {
            "toolCallId": identifier, "success": success,
            "result": {"structuredContent": result},
        }, actor=actor))

    def start_agent(self, identifier, role, actor):
        self.tool_start(identifier, "task")
        self.run.observe(event("subagent.started", {
            "toolCallId": identifier, "agentName": role,
        }, actor=actor))

    def end_agent(self, identifier, role, actor, failed=False):
        self.run.observe(event("subagent.failed" if failed else "subagent.completed", {
            "toolCallId": identifier, "agentName": role, "totalTokens": 99999,
        }, actor=actor))
        self.tool_end(identifier)

    def test_usage_deduplicates_events_and_api_calls_without_adding_breakdowns(self):
        first = self.usage(apiCallId="call1", cacheReadTokens=60, cacheWriteTokens=10, reasoningTokens=7)
        self.run.observe(first)
        self.run.observe(first)
        self.run.observe(self.usage(apiCallId="call1"))
        self.run.observe(self.usage(apiCallId="call2"))
        result = self.complete()
        self.assertEqual(result["usage_calls"], 2)
        self.assertEqual(result["input_tokens"], 200)
        self.assertEqual(result["output_tokens"], 40)
        self.assertEqual(result["cache_read_tokens"], 60)
        self.assertEqual(result["reasoning_tokens"], 7)
        self.assertEqual(result["cache_read_tokens_calls"], 1)
        self.assertEqual(result["usage_status"], "complete")
        model, = self.backend.facts("issuelens.run.model")
        self.assertEqual(model["input_tokens"], 200)

    def test_actual_sdk_events_use_timedelta_timing(self):
        self.clock.value = 3
        self.run.observe(SessionEvent(
            id=uuid.uuid4(), timestamp=datetime.now(timezone.utc),
            type=SessionEventType.ASSISTANT_USAGE,
            data=AssistantUsageData(
                model="gpt-test", input_tokens=12, output_tokens=4,
                duration=timedelta(seconds=2), time_to_first_token=timedelta(milliseconds=250),
            ),
        ))
        result = self.complete()
        self.assertEqual(result["input_tokens"], 12)
        timings = [(name, value) for name, value, _ in self.backend.metrics]
        self.assertIn(("issuelens.model.time_to_first_token", 0.25), timings)
        self.assertIn(("gen_ai.client.operation.duration", 2.0), timings)
        span, = [span for span in self.backend.exporter.get_finished_spans() if span.name == "chat"]
        self.assertEqual(span.end_time - span.start_time, 2_000_000_000)

    def test_sdk_wire_decoder_preserves_tokens_and_millisecond_timings(self):
        self.clock.value = 3
        decoded = SessionEvent.from_dict({
            "id": str(uuid.uuid4()), "timestamp": "2026-09-15T12:00:00Z",
            "type": "assistant.usage", "data": {
                "model": "gpt-test", "inputTokens": 12, "outputTokens": 4,
                "duration": 2000, "timeToFirstTokenMs": 250,
            },
        })
        self.run.observe(decoded)
        result = self.complete()
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (12, 4))
        self.assertIn(("issuelens.model.time_to_first_token", 0.25),
                      [(name, value) for name, value, _ in self.backend.metrics])
        dimensions = next(attrs for name, _, attrs in self.backend.metrics if name == "gen_ai.client.token.usage")
        self.assertEqual(dimensions["gen_ai.token.type"], "input")
        self.assertEqual(dimensions["gen_ai.request.model"], "gpt-test")
        self.assertEqual(dimensions["gen_ai.agent.name"], "issuelens")
        self.assertEqual(dimensions["gen_ai.operation.name"], "chat")
        self.assertNotIn("model", dimensions)

    def test_failed_model_call_has_a_sanitized_timed_diagnostic_span(self):
        self.clock.value = 3
        self.run.observe(SessionEvent.from_dict({
            "id": str(uuid.uuid4()), "timestamp": "2026-09-15T12:00:00Z",
            "type": "model.call_failure", "data": {
                "source": next(iter(ModelCallFailureSource)).value,
                "model": "gpt-test", "durationMs": 1500, "statusCode": 429,
                "errorMessage": "PRIVATE-CANARY",
            },
        }))
        result = self.complete()
        self.assertEqual(result["model_failures"], 1)
        span, = [span for span in self.backend.exporter.get_finished_spans() if span.name == "chat"]
        self.assertEqual(span.end_time - span.start_time, 1_500_000_000)
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertEqual(span.attributes["error.type"], "rate_limit")
        self.assertNotIn("PRIVATE-CANARY", str(span.attributes))

    def test_active_runs_balance_once_and_rejections_never_increment_active(self):
        self.run.admit()
        self.complete()
        self.run.finish("completed")
        active = [value for name, value, _ in self.backend.metrics if name == "issuelens.run.active"]
        self.assertEqual(active, [1, -1])
        counts = [value for name, value, _ in self.backend.metrics if name == "issuelens.run.count"]
        self.assertEqual(counts, [1])
        other = RunTelemetry(self.backend, self.run.settings, "invocations")
        other.finish("rejected", stage="validation", error_type="invalid_input")
        other.admit()
        self.assertFalse(other.admitted)
        self.assertEqual(active, [value for name, value, _ in self.backend.metrics if name == "issuelens.run.active"])

    def test_missing_usage_and_context_pressure_are_not_billed_tokens(self):
        self.run.observe(event("session.usage_info", {"currentTokens": 9000, "tokenLimit": 10000}))
        result = self.complete()
        self.assertEqual(result["usage_status"], "unavailable")
        self.assertNotIn("input_tokens", result)
        self.assertEqual(result["context_tokens_peak"], 9000)
        self.assertEqual(result["context_token_limit"], 10000)

    def test_missing_fields_and_failed_attempts_make_usage_partial(self):
        self.run.observe(self.usage(outputTokens=None, inputTokens=True))
        self.run.observe(event("model.call_failure", {"statusCode": 429, "errorMessage": "SECRET"}))
        self.run.observe(event("assistant.turn_retry"))
        result = self.complete()
        self.assertEqual(result["usage_status"], "partial")
        self.assertNotIn("input_tokens", result)
        self.assertNotIn("output_tokens", result)
        self.assertEqual(result["model_failures"], 1)
        self.assertEqual(result["model_retries"], 1)
        self.assertNotIn("SECRET", json.dumps(self.backend.events))

    def test_exclusive_agent_usage_and_parallel_tool_ids(self):
        for actor, role in (("worker1", "triage"), ("worker2", "plan")):
            self.start_agent(f"task-{actor}", role, actor)
            self.run.observe(event("assistant.usage", {
                "model": "gpt-test", "inputTokens": 5, "outputTokens": 2,
            }, actor=actor))
            self.tool_start("same-local-id", actor=actor)
            self.clock.value += 1
            self.tool_end("same-local-id", actor=actor, number=42)
            self.end_agent(f"task-{actor}", role, actor)
        self.run.observe(self.usage())
        result = self.complete()
        self.assertEqual(result["input_tokens"], 110)
        self.assertEqual(result["tools_started"], 4)
        self.assertEqual(result["agents_started"], 2)
        roles = {fact["role"]: fact for fact in self.backend.facts("issuelens.run.agent")}
        self.assertEqual(roles["issuelens"]["input_tokens"], 100)
        self.assertEqual(roles["triage"]["input_tokens"], 5)
        self.assertEqual(roles["plan"]["input_tokens"], 5)
        self.assertEqual(roles["triage"]["duration_s"], 1)
        self.assertTrue(result["attribution_complete"])
        self.assertNotIn(99999, [fact.get("input_tokens") for fact in roles.values()])

    def test_legacy_parent_binding_and_late_events(self):
        self.start_agent("task1", "plan", "root")
        self.run.observe(self.usage(parentToolCallId="task1"))
        self.end_agent("task1", "plan", "root")
        self.complete()
        facts = self.backend.facts("issuelens.run.agent")
        self.assertEqual(next(fact for fact in facts if fact["role"] == "plan")["input_tokens"], 100)
        before = len(self.backend.events)
        self.run.observe(self.usage())
        self.run.finish("failed")
        self.assertEqual(len(self.backend.events), before)

    def test_legacy_api_and_event_ids_are_scoped_to_the_canonical_owner(self):
        identifier = str(uuid.uuid4())
        for call, role in (("task1", "triage"), ("task2", "plan")):
            self.start_agent(call, role, "root")
            self.run.observe(event("assistant.usage", {
                "apiCallId": "same-api-id", "model": "gpt-test",
                "inputTokens": 10, "outputTokens": 2, "parentToolCallId": call,
            }, identifier=identifier))
        self.run.observe(event("assistant.usage", {
            "apiCallId": "same-api-id", "model": "gpt-test",
            "inputTokens": 10, "outputTokens": 2, "parentToolCallId": "task1",
        }, actor="worker-alias"))
        self.end_agent("task1", "triage", "root")
        self.end_agent("task2", "plan", "root")
        result = self.complete()
        self.assertEqual(result["usage_calls"], 2)
        self.assertEqual(result["input_tokens"], 20)
        self.assertTrue(result["attribution_complete"])

    def test_nested_delegation_through_an_alias_does_not_overwrite_its_parent(self):
        self.start_agent("task1", "triage", "root")
        self.run.observe(event("assistant.usage", {
            "model": "gpt-test", "inputTokens": 10, "outputTokens": 2,
            "parentToolCallId": "task1",
        }, actor="parent-alias"))
        self.tool_start("nested", "task", actor="parent-alias")
        self.run.observe(event("subagent.started", {
            "toolCallId": "nested", "agentName": "plan",
        }, actor="parent-alias"))
        self.run.observe(event("assistant.usage", {
            "model": "gpt-test", "inputTokens": 5, "outputTokens": 1,
            "parentToolCallId": "nested",
        }, actor="child-alias"))
        self.clock.value = 2
        self.run.observe(event("subagent.completed", {"toolCallId": "nested"}, actor="parent-alias"))
        self.tool_end("nested", actor="parent-alias")
        self.end_agent("task1", "triage", "root")
        result = self.complete()
        roles = {fact["role"]: fact for fact in self.backend.facts("issuelens.run.agent")}
        self.assertEqual(roles["triage"]["input_tokens"], 10)
        self.assertEqual(roles["plan"]["input_tokens"], 5)
        self.assertEqual(roles["plan"]["parent_agent_id"], roles["triage"]["agent_run_id"])
        self.assertEqual(result["agents_started"], 2)
        self.assertTrue(result["attribution_complete"])
        for span in self.backend.exporter.get_finished_spans():
            self.assertGreaterEqual(span.end_time, span.start_time, span.name)

    def test_shared_delegation_ids_use_parent_scope_and_never_guess_legacy_ownership(self):
        for parent, role in (("parent1", "triage"), ("parent2", "plan")):
            self.start_agent(f"task-{parent}", role, parent)
            self.tool_start("shared", "task", actor=parent)
            self.run.observe(event("subagent.started", {
                "toolCallId": "shared", "agentName": "find-criticals",
            }, actor=parent))
        self.run.observe(self.usage(parentToolCallId="shared"))
        for parent, role in (("parent1", "triage"), ("parent2", "plan")):
            self.run.observe(event("subagent.completed", {"toolCallId": "shared"}, actor=parent))
            self.tool_end("shared", actor=parent)
            self.end_agent(f"task-{parent}", role, parent)
        result = self.complete()
        children = [f for f in self.backend.facts("issuelens.run.agent") if f["role"] == "find-criticals"]
        self.assertEqual({f["parent_agent_id"] for f in children}, {"parent1", "parent2"})
        self.assertTrue(all(f["status"] == "completed" for f in children))
        self.assertTrue(all("input_tokens" not in f for f in children))
        self.assertFalse(result["attribution_complete"])
        self.assertIn("incomplete_ambiguous_delegation", result)

    def test_ambiguous_start_cannot_bind_new_child_usage_to_an_existing_child(self):
        for parent in ("parent1", "parent2"):
            self.start_agent(f"task-{parent}", "triage", parent)
        self.tool_start("shared", "task", actor="parent1")
        self.run.observe(event("subagent.started", {
            "toolCallId": "shared", "agentName": "plan",
        }, actor="child1"))
        self.tool_start("shared", "task", actor="parent2")
        self.run.observe(event("subagent.started", {
            "toolCallId": "shared", "agentName": "plan",
        }, actor="child2"))
        self.run.observe(event("assistant.usage", {
            "model": "gpt-test", "inputTokens": 10, "parentToolCallId": "shared",
        }, actor="child2"))
        result = self.complete()
        original = next(f for f in self.backend.facts("issuelens.run.agent") if f["agent_run_id"] == "child1")
        self.assertNotIn("input_tokens", original)
        self.assertFalse(result["attribution_complete"])

    def test_invalid_actor_and_unresolved_legacy_owner_are_not_root_usage(self):
        self.run.observe(self.usage(parentToolCallId="missing"))
        self.run.observe(event("assistant.usage", {
            "model": "gpt-test", "inputTokens": 5,
        }, actor="PRIVATE@CANARY"))
        result = self.complete()
        root = next(f for f in self.backend.facts("issuelens.run.agent") if f["role"] == "issuelens")
        self.assertNotIn("input_tokens", root)
        self.assertEqual(result["input_tokens"], 105)
        self.assertEqual(result["agents_started"], 0)
        self.assertFalse(result["attribution_complete"])
        self.assertNotIn("PRIVATE@CANARY", str(self.backend.events))

    def test_alias_cache_remains_bounded_without_losing_known_usage(self):
        self.run.MAX_AGENTS = 3
        self.start_agent("task1", "plan", "root")
        for index in range(10):
            self.run.observe(event("assistant.usage", {
                "model": "gpt-test", "inputTokens": 10, "outputTokens": 2,
                "parentToolCallId": "task1",
            }, actor=f"alias{index}"))
        self.end_agent("task1", "plan", "root")
        result = self.complete()
        self.assertLessEqual(len(self.run.agents), self.run.MAX_AGENTS)
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["agents_started"], 1)

    def test_overflow_usage_does_not_inflate_the_count_of_tracked_subagents(self):
        self.run.MAX_AGENTS = 2
        self.start_agent("task1", "plan", "worker")
        self.run.observe(event("assistant.usage", {
            "model": "gpt-test", "inputTokens": 10, "outputTokens": 2,
        }, actor="worker"))
        self.run.observe(event("assistant.usage", {
            "model": "gpt-test", "inputTokens": 5, "outputTokens": 1,
        }, actor="overflow"))
        self.end_agent("task1", "plan", "worker")
        result = self.complete()
        self.assertEqual(result["agents_started"], 1)
        self.assertEqual(result["input_tokens"], 15)
        self.assertFalse(result["attribution_complete"])
        bucket = next(fact for fact in self.backend.facts("issuelens.run.agent")
                      if fact["agent_run_id"] == "unattributed")
        self.assertEqual(bucket["input_tokens"], 5)

    def test_first_output_excludes_lifecycle_hidden_nested_and_empty_content(self):
        def message(identifier, phase, text, actor=None):
            self.run.observe(event("assistant.message_start", {"messageId": identifier, "phase": phase}, actor=actor))
            item = event("assistant.message_delta", {"messageId": identifier, "deltaContent": text}, actor=actor)
            self.run.observe(item)
            self.run.visible_output(item)

        self.clock.value = 1
        message("analysis", "analysis", "HIDDEN")
        message("nested", "final", "nested", actor="worker")
        message("empty", "final", "")
        self.run.visible_output(event("assistant.message_delta", {"messageId": "no-start", "deltaContent": "unknown phase"}))
        self.assertIsNone(self.run.first_output)
        self.clock.value = 2
        message("progress", "commentary", "Checking")
        self.clock.value = 5
        message("answer", "final", "Done")
        result = self.complete()
        self.assertEqual(result["first_root_output_s"], 2)
        self.assertEqual(result["first_final_output_s"], 5)

    def test_repeated_message_start_cannot_reenable_analysis(self):
        for phase in ("analysis", "final"):
            self.run.observe(event("assistant.message_start", {"messageId": "hidden", "phase": phase}))
        self.run.visible_output(event("assistant.message_delta", {"messageId": "hidden", "deltaContent": "SECRET"}))
        self.assertNotIn("first_root_output_s", self.complete())

    def test_targets_distinguish_type_repository_and_repeated_reads(self):
        for identifier, repo, pull in (("one", "Org/Repo", False), ("two", "Org/Repo", False),
                                       ("three", "Other/Repo", False), ("four", "PR/Repo", True)):
            self.tool_start(identifier, repository=repo)
            self.tool_end(identifier, number=42, **({"pull_request": {}} if pull else {}))
        self.complete()
        facts = [fact for fact in self.backend.facts("issuelens.run.target") if fact["relationship"] == "read"]
        self.assertEqual(len(facts), 3)
        self.assertEqual(next(f for f in facts if f["repository"] == "org/repo")["operations"], 2)
        self.assertEqual(next(f for f in facts if f["repository"] == "pr/repo")["target_kind"], "pull_request")

    def test_reaction_targets_use_issue_and_pull_request_numbers(self):
        expected = set()
        for kind, number in (("issue", 42), ("pull_request", 43)):
            self.run.observe(event("tool.execution_start", {
                "toolCallId": kind, "toolName": "github-add_eyes_reaction",
                "arguments": {"repository": "Org/Repo", "target_kind": kind, "target_id": number},
            }))
            self.tool_end(kind, id=999)
            for relationship in ("attempted", "write_succeeded"):
                expected.add(("org/repo", kind, number, relationship))
        result = self.complete()
        targets = self.backend.facts("issuelens.run.target")
        self.assertEqual({
            (fact["repository"], fact["target_kind"], fact["number"], fact["relationship"])
            for fact in targets
        }, expected)
        self.assertTrue(all(fact["operations"] == 1 for fact in targets))
        self.assertEqual(result["write_operations_succeeded"], 2)

    def test_comment_reaction_ids_are_not_issue_or_pull_request_numbers(self):
        for kind in ("issue_comment", "pull_request_review_comment"):
            self.run.observe(event("tool.execution_start", {
                "toolCallId": kind, "toolName": "github-add_eyes_reaction",
                "arguments": {"repository": "Org/Repo", "target_kind": kind, "target_id": 98765},
            }))
            self.tool_end(kind, id=999)
        result = self.complete()
        targets = self.backend.facts("issuelens.run.target")
        self.assertTrue(targets)
        self.assertTrue(all(fact["target_kind"] == "repository" and fact["number"] == 0 for fact in targets))
        self.assertEqual(result["write_operations_succeeded"], 2)

    def test_failed_reaction_retains_typed_attempt_without_confirming_a_write(self):
        self.run.observe(event("tool.execution_start", {
            "toolCallId": "reaction", "toolName": "github-add_eyes_reaction",
            "arguments": {"repository": "Org/Repo", "target_kind": "pull_request", "target_id": 42},
        }))
        self.tool_end("reaction", success=False)
        result = self.complete()
        target, = self.backend.facts("issuelens.run.target")
        self.assertEqual((target["target_kind"], target["number"], target["relationship"]),
                         ("pull_request", 42, "attempted"))
        self.assertEqual(result["write_operations_succeeded"], 0)

    def test_search_results_do_not_count_individual_issues(self):
        self.run.observe(event("tool.execution_start", {
            "toolCallId": "search", "toolName": "github-search_issues",
            "arguments": {"repository": "Org/Repo", "query": "SECRET"},
        }))
        self.tool_end("search", items=[{"number": 1}, {"number": 2}], total_count=100)
        self.complete()
        self.assertTrue(all(fact["target_kind"] == "repository" for fact in self.backend.facts("issuelens.run.target")))

    def test_repository_identity_and_host_reads_enrich_without_double_counting_targets(self):
        self.run.host_issue_read("Org/Repo", 42)
        self.tool_start("metadata", "github-get_repository", issue_number=None)
        self.tool_end("metadata", id=123, full_name="ORG/REPO")
        self.tool_start()
        self.tool_end(number=42, id=999)
        self.complete()
        facts = self.backend.facts("issuelens.run.target")
        self.assertTrue(all(f["repository_id"] == 123 for f in facts))
        self.assertFalse(any(f["target_kind"] == "work_item" for f in facts))
        issues = [f for f in facts if f["target_kind"] == "issue" and f["relationship"] == "read"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["operations"], 2)

    def test_wiki_destination_is_separate_from_source(self):
        self.tool_start("wiki", "wiki-writer-write_wiki_pages", issue_number=None)
        self.tool_end("wiki", source_repository="org/repo", wiki_repository="org/memory", status="no-change")
        result = self.complete()
        facts = self.backend.facts("issuelens.run.target")
        self.assertFalse(any(f["repository"] == "org/repo" and f["relationship"] == "write_succeeded" for f in facts))
        self.assertTrue(any(f["repository"] == "org/memory" and f["target_kind"] == "wiki" for f in facts))
        self.assertEqual(result["write_operations_succeeded"], 1)

    def test_wiki_transport_success_without_a_confirmed_status_is_not_a_write(self):
        self.tool_start("wiki", "wiki-writer-write_wiki_pages", issue_number=None)
        self.tool_end("wiki", source_repository="org/repo", wiki_repository="org/memory", status="pending")
        result = self.complete()
        self.assertEqual(result["write_operations_succeeded"], 0)
        self.assertEqual(result["business_outcome"], "unknown")
        self.assertEqual(result["incomplete_unconfirmed_wiki_write"], 1)
        self.assertFalse(any(f["relationship"] == "write_succeeded"
                             for f in self.backend.facts("issuelens.run.target")))

    def test_tool_error_payload_is_failure_despite_transport_success(self):
        self.tool_start("write", "github-add_labels", labels=["SECRET"])
        self.tool_end("write", isError=True)
        result = self.complete()
        self.assertEqual(result["tools_failed"], 1)
        self.assertEqual(result["write_operations_succeeded"], 0)
        span, = [span for span in self.backend.exporter.get_finished_spans() if span.name.startswith("execute_tool")]
        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertIn("failed", [attrs.get("status") for _, _, attrs in self.backend.metrics])

    def test_successful_write_then_failure_is_partial_not_success(self):
        self.tool_start("write", "github-add_issue_comment", body="SECRET")
        self.tool_end("write", id=123)
        self.run.observe(event("session.error", {"message": "SECRET", "statusCode": 500}))
        result = self.complete()
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["transport_status"], "completed")
        self.assertEqual(result["business_outcome"], "partial")

    def test_content_and_free_form_errors_are_not_exported(self):
        self.tool_start("notify", "send-email", body="PRIVATE-CANARY", recipients=["alice@example.invalid"])
        self.run.observe(event("tool.execution_complete", {
            "toolCallId": "notify", "success": False,
            "error": {"message": "https://secret.invalid/?sig=PRIVATE-CANARY"},
            "result": {"content": "PRIVATE-CANARY"},
        }))
        self.run.observe(event("assistant.reasoning_delta", {"deltaContent": "PRIVATE-CANARY"}))
        self.complete()
        exported = json.dumps(self.backend.events) + str([
            dict(span.attributes) for span in self.backend.exporter.get_finished_spans()
        ])
        self.assertNotIn("PRIVATE-CANARY", exported)
        self.assertNotIn("alice@", exported)
        self.assertNotIn("sig=", exported)
        for _, _, dimensions in self.backend.metrics:
            self.assertFalse({"repository", "run_id", "number", "session_id"} & dimensions.keys())

    def test_cancelled_unfinished_calls_are_explicit(self):
        self.tool_start()
        self.run.finish("cancelled", error_type="cancelled", transport_status="interrupted")
        result = self.backend.facts("issuelens.run.completed")[-1]
        self.assertTrue(result["telemetry_incomplete"])
        self.assertEqual(result["incomplete_unfinished_tool"], 1)
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertEqual(result["tools_completed"], 0)

    def test_tracking_limits_do_not_silently_report_complete_counts(self):
        self.run.MAX_EVENTS = 2
        for _ in range(4):
            self.run.observe(self.usage())
        result = self.complete()
        self.assertEqual(len(self.run.seen), 2)
        self.assertEqual(result["incomplete_event_limit"], 2)
        self.assertEqual(result["usage_status"], "partial")

    def test_each_state_budget_is_bounded_and_reports_incompleteness(self):
        self.run.MAX_TOOLS = self.run.MAX_TARGETS = self.run.MAX_MESSAGES = 1
        self.run.MAX_AGENTS = self.run.MAX_MODELS = 1
        self.run.host_issue_read("org/repo", 42)
        self.tool_start("first")
        self.tool_start("second")
        self.tool_end("first", number=42)
        for index in range(3):
            self.run.observe(self.usage(model=f"model{index}"))
            self.run.observe(event("assistant.message_start", {"messageId": f"message{index}"}))
        self.run.observe(event("subagent.started", {"toolCallId": "task", "agentName": "plan"}, actor="worker"))
        self.run.observe(event("assistant.usage", {"model": "model0", "inputTokens": 5}, actor="worker"))
        result = self.complete()
        for name in ("tools", "targets", "phases"):
            self.assertLessEqual(len(getattr(self.run, name)), 1, name)
        self.assertLessEqual(len(self.run.models), 2)  # One named model and its overflow bucket.
        self.assertLessEqual(len(self.run.agents), 2)  # Root plus unattributed overflow.
        for reason in ("tool_limit", "target_limit", "message_limit", "model_limit", "agent_limit"):
            self.assertGreater(result[f"incomplete_{reason}"], 0, reason)
        self.assertEqual(result["input_tokens"], 305)
        self.assertEqual(result["agents_started"], 0)
        self.assertTrue(result["telemetry_incomplete"])

    def test_unknown_agent_is_not_attributed_to_root(self):
        self.run.observe(event("assistant.usage", {"model": "gpt-test", "inputTokens": 5}, actor="unknown"))
        result = self.complete()
        self.assertFalse(result["attribution_complete"])
        root = next(f for f in self.backend.facts("issuelens.run.agent") if f["role"] == "issuelens")
        self.assertNotIn("input_tokens", root)
        self.assertEqual(result["input_tokens"], 5)
        self.assertEqual(result["agents_started"], 1)

    def test_export_rejection_is_reported_without_raising(self):
        self.backend.reject_events = True
        self.run.observe(self.usage())
        result = self.complete()
        self.assertTrue(result["telemetry_incomplete"])
        self.assertGreater(result["incomplete_export_rejected"], 0)

    def test_default_settings_collect_telemetry_without_opt_in(self):
        backend = RecordingBackend()
        self.addCleanup(backend.provider.shutdown)
        run = RunTelemetry(backend, Settings(), "invocations")
        run.admit()
        run.model_sent = True
        run.observe(self.usage())
        run.finish("completed")
        summary, = backend.facts("issuelens.run.completed")
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["usage_status"], "complete")
        self.assertEqual(summary["release"], "unknown")
        self.assertTrue(backend.metrics)
        self.assertTrue(backend.exporter.get_finished_spans())
        for _, attributes in backend.events:
            self.assertNotIn("environment", attributes)
        for _, _, attributes in backend.metrics:
            self.assertNotIn("environment", attributes)

    def test_new_turns_have_new_accounting_and_trace_identity(self):
        self.run.observe(self.usage())
        first = self.complete()
        other = RunTelemetry(self.backend, self.run.settings, "responses", identifiers={"conversation_id": "conversation1"})
        other.admit()
        other.model_sent = True
        other.observe(self.usage(inputTokens=5))
        other.finish("completed")
        second = self.backend.facts("issuelens.run.completed")[-1]
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["run_trace_id"], second["run_trace_id"])
        self.assertEqual(second["input_tokens"], 5)


class SettingsTests(unittest.TestCase):
    def test_release_has_a_default_and_rejects_invalid_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(prepare_environment(), Settings())
            self.assertEqual(os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")
        with patch.dict(os.environ, {"ISSUELENS_RELEASE": "release-1.2"}, clear=True):
            self.assertEqual(Settings.from_environment(), Settings(release="release-1.2"))
        with patch.dict(os.environ, {"ISSUELENS_RELEASE": "private\ncontent"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_environment()

    def test_content_capture_is_always_disabled_before_host_initialization(self):
        with patch.dict(os.environ, {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true"}, clear=True):
            prepare_environment()
            self.assertEqual(os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")

    def test_cli_export_isolated_without_mutating_process_environment(self):
        original = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://receiver.invalid",
                    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=SECRET", "GITHUB_TOKEN": "MODEL_TOKEN"}
        with patch.dict(os.environ, original, clear=True):
            environment = copilot_environment()
            self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", environment)
            self.assertNotIn("OTEL_EXPORTER_OTLP_HEADERS", environment)
            self.assertEqual(environment["COPILOT_OTEL_ENABLED"], "false")
            self.assertEqual(environment["GITHUB_TOKEN"], "MODEL_TOKEN")
            self.assertEqual(dict(os.environ), original)

    def test_result_metadata_does_not_copy_content_or_scan_prose(self):
        self.assertEqual(result_metadata({"content": "I updated SECRET repository X/Y"}), {})
        result = result_metadata({"structuredContent": {"number": 7, "body": "SECRET", "pull_request": {}}})
        self.assertEqual(result, {"number": 7, "is_pull_request": True, "is_error": False})
        self.assertEqual(result_metadata({"content": "[" * (128 * 1024 + 1)}), {})

    def test_outer_mcp_errors_survive_missing_unparseable_and_nested_content(self):
        for content in (None, "PRIVATE-CANARY", "[" * (128 * 1024 + 1), "[]"):
            self.assertEqual(result_metadata({"isError": True, "content": content}), {"is_error": True})
        nested = {"isError": True, "structuredContent": {"number": 7, "body": "PRIVATE-CANARY"}}
        self.assertEqual(result_metadata({"content": json.dumps(nested)}),
                         {"number": 7, "is_pull_request": False, "is_error": True})


if __name__ == "__main__":
    unittest.main()
