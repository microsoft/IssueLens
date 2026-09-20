import unittest
from unittest.mock import Mock, patch

from azure.ai.agentserver.core._tracing import (
    _BaggageLogRecordProcessor, _FoundryEnrichmentSpanProcessor,
)
from azure.monitor.opentelemetry.exporter.export.logs._exporter import _convert_log_to_envelope
from azure.monitor.opentelemetry.exporter.export.logs._processor import _AzureBatchLogRecordProcessor
from microsoft.opentelemetry._genai.main_agent import (
    GenAIMainAgentLogRecordProcessor, GenAIMainAgentSpanProcessor,
)
from opentelemetry import baggage, context, trace
from opentelemetry._logs import LogRecord, NoOpLogger, NoOpLoggerProvider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

from telemetry_export import OpenTelemetryBackend


class TelemetryExportTests(unittest.TestCase):
    def setUp(self):
        self.logs = InMemoryLogRecordExporter()
        self.log_provider = LoggerProvider()
        self.log_provider.add_log_record_processor(SimpleLogRecordProcessor(self.logs))
        self.traces = TracerProvider(sampler=ALWAYS_OFF)
        self.reader = InMemoryMetricReader()
        self.meters = MeterProvider(metric_readers=[self.reader])
        self.addCleanup(self.log_provider.shutdown)
        self.addCleanup(self.traces.shutdown)
        self.addCleanup(self.meters.shutdown)
        self.backend = OpenTelemetryBackend(
            event_logger=self.log_provider.get_logger("issuelens"),
            tracer=self.traces.get_tracer("issuelens"),
            meter=self.meters.get_meter("issuelens"),
        )

    def test_bi_records_have_independent_context_and_explicit_run_correlation(self):
        with self.traces.get_tracer("host").start_as_current_span("dropped diagnostic trace") as parent:
            self.assertFalse(parent.get_span_context().trace_flags.sampled)
            run_trace = format(parent.get_span_context().trace_id, "032x")
            self.assertTrue(self.backend.event("issuelens.run.completed", {
                "run_id": "run1", "run_trace_id": run_trace, "execution_status": "completed",
            }))
        record = self.logs.get_finished_logs()[0].log_record
        self.assertEqual(record.trace_id, 0)
        self.assertEqual(record.span_id, 0)
        self.assertEqual(record.attributes["run_trace_id"], run_trace)
        self.assertEqual(record.attributes["microsoft.custom_event.name"], "issuelens.run.completed")

    def test_azure_exporter_maps_facts_to_custom_events_with_flat_dimensions(self):
        self.backend.event("issuelens.run.completed", {
            "schema_version": "1", "run_id": "run1", "input_tokens": 123,
            "telemetry_incomplete": False, "duration_s": 1.25,
        })
        envelope = _convert_log_to_envelope(self.logs.get_finished_logs()[0])
        self.assertEqual(envelope.data.base_type, "EventData")
        self.assertEqual(envelope.data.base_data.name, "issuelens.run.completed")
        self.assertEqual(envelope.data.base_data.properties["run_id"], "run1")
        self.assertEqual(envelope.data.base_data.properties["input_tokens"], "123")
        self.assertEqual(envelope.data.base_data.properties["duration_s"], "1.25")

    def test_host_processors_and_trace_based_sampling_keep_only_independent_bi_fact(self):
        logs = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        self.addCleanup(provider.shutdown)
        provider.add_log_record_processor(GenAIMainAgentLogRecordProcessor())
        provider.add_log_record_processor(_BaggageLogRecordProcessor(agent_name="hosted-agent"))
        provider.add_log_record_processor(_AzureBatchLogRecordProcessor(
            logs, {"enable_trace_based_sampling_for_logs": True},
        ))
        event_logger = provider.get_logger("issuelens")
        backend = OpenTelemetryBackend(event_logger=event_logger)
        parent_context = baggage.set_baggage("private", "PRIVATE-CANARY")
        parent_context = baggage.set_baggage("run_id", "spoofed", context=parent_context)
        parent_context = baggage.set_baggage(
            "microsoft.custom_event.name", "spoofed", context=parent_context,
        )
        token = context.attach(parent_context)
        try:
            with self.traces.get_tracer("host").start_as_current_span("not sampled") as parent:
                event_logger.emit(LogRecord(body="sampled-out diagnostic"))
                backend.event("issuelens.run.completed", {"run_id": "real-run"})
                self.assertIs(trace.get_current_span(), parent)
                self.assertEqual(baggage.get_baggage("private"), "PRIVATE-CANARY")
        finally:
            context.detach(token)
        self.assertTrue(provider.force_flush())
        record, = logs.get_finished_logs()
        envelope = _convert_log_to_envelope(record)
        self.assertEqual(envelope.data.base_type, "EventData")
        self.assertEqual(envelope.data.base_data.name, "issuelens.run.completed")
        self.assertEqual(envelope.data.base_data.properties["run_id"], "real-run")
        self.assertNotIn("private", record.log_record.attributes)
        self.assertNotIn("PRIVATE-CANARY", str(envelope))
        self.assertEqual(record.log_record.trace_id, 0)
        self.assertEqual(record.log_record.span_id, 0)

    def test_diagnostic_parent_and_logical_role_survive_host_enrichment_without_baggage(self):
        spans = InMemorySpanExporter()
        provider = TracerProvider()
        self.addCleanup(provider.shutdown)
        provider.add_span_processor(GenAIMainAgentSpanProcessor())
        provider.add_span_processor(_FoundryEnrichmentSpanProcessor(agent_name="hosted-agent"))
        provider.add_span_processor(SimpleSpanProcessor(spans))
        tracer = provider.get_tracer("issuelens")
        backend = OpenTelemetryBackend(tracer=tracer)
        with tracer.start_as_current_span("host", attributes={
            "gen_ai.conversation.id": "PRIVATE-CANARY",
        }) as parent:
            token = context.attach(baggage.set_baggage("gen_ai.conversation.id", "PRIVATE-CANARY"))
            try:
                span = backend.span("invoke_agent plan", {
                    "gen_ai.operation.name": "invoke_agent", "issuelens.agent.role": "plan",
                })
                span.end()
                self.assertIs(trace.get_current_span(), parent)
            finally:
                context.detach(token)
        span = next(item for item in spans.get_finished_spans() if item.name == "invoke_agent plan")
        self.assertEqual(span.parent.span_id, parent.get_span_context().span_id)
        self.assertEqual(span.attributes["gen_ai.agent.name"], "hosted-agent")
        self.assertEqual(span.attributes["issuelens.agent.role"], "plan")
        self.assertNotIn("PRIVATE-CANARY", str(span.attributes))

    def test_active_run_counter_accepts_decrements_without_negative_histograms(self):
        for value in (1, 1, -1):
            self.backend.metric("issuelens.run.active", value, {"protocol": "responses"})
        resource, = self.reader.get_metrics_data().resource_metrics
        scope, = resource.scope_metrics
        metric, = scope.metrics
        point, = metric.data.data_points
        self.assertEqual(point.value, 1)
        self.assertFalse(metric.data.is_monotonic)
        with self.assertLogs("issuelens.telemetry.export", level="WARNING"):
            self.backend.metric("issuelens.run.duration", -1, {})

    def test_metrics_remain_available_when_diagnostic_trace_is_not_sampled(self):
        with self.traces.get_tracer("host").start_as_current_span("not sampled"):
            for tokens in (100, 50):
                self.backend.metric("gen_ai.client.token.usage", tokens, {"token_type": "input", "model": "test"})
        resource, = self.reader.get_metrics_data().resource_metrics
        scope, = resource.scope_metrics
        metric, = scope.metrics
        point, = metric.data.data_points
        self.assertEqual(metric.name, "gen_ai.client.token.usage")
        self.assertEqual(point.sum, 150)
        self.assertEqual(point.count, 2)

    def test_backend_reuses_injected_providers_and_never_sets_globals(self):
        with patch("opentelemetry.trace.set_tracer_provider") as set_tracer, \
                patch("opentelemetry.metrics.set_meter_provider") as set_meter, \
                patch("opentelemetry._logs.set_logger_provider") as set_logger:
            self.backend.event("issuelens.run.started", {"run_id": "run1"})
            self.backend.metric("issuelens.model.retries", 1, {"protocol": "responses"})
            self.backend.span("invoke_agent issuelens", {"gen_ai.operation.name": "invoke_agent"}).end()
        set_tracer.assert_not_called()
        set_meter.assert_not_called()
        set_logger.assert_not_called()

    def test_logger_failures_are_bounded_sanitized_and_nonfatal(self):
        self.backend.event_logger = Mock()
        self.backend.event_logger.emit.side_effect = RuntimeError("PRIVATE-CANARY ?sig=SECRET")
        with self.assertLogs("issuelens.telemetry.export", level="WARNING") as logs:
            for _ in range(3):
                self.assertFalse(self.backend.event("issuelens.run.completed", {"run_id": "run1"}))
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("PRIVATE", logs.output[0])
        self.assertNotIn("SECRET", logs.output[0])
        self.assertEqual(self.backend.failures, 3)

    def test_noop_logger_is_not_reported_as_accepted_export(self):
        backend = OpenTelemetryBackend(event_logger=NoOpLogger("noop"))
        with self.assertLogs("issuelens.telemetry.export", level="WARNING"):
            self.assertFalse(backend.event("issuelens.run.started", {"run_id": "run1"}))
        with patch("telemetry_export.get_logger_provider", return_value=NoOpLoggerProvider()):
            backend = OpenTelemetryBackend()
            with self.assertLogs("issuelens.telemetry.export", level="WARNING"):
                self.assertFalse(backend.event("issuelens.run.started", {"run_id": "run1"}))

    def test_metric_and_span_exporter_errors_cannot_fail_the_agent(self):
        self.backend.tracer = Mock()
        self.backend.tracer.start_span.side_effect = RuntimeError("PRIVATE-CANARY")
        self.backend.meter = Mock()
        self.backend.meter.create_histogram.side_effect = RuntimeError("PRIVATE-CANARY")
        with self.assertLogs("issuelens.telemetry.export", level="WARNING") as logs:
            self.backend.metric("issuelens.run.duration", 1, {"protocol": "responses"})
            self.assertIs(self.backend.span("safe name", {}), trace.INVALID_SPAN)
        self.assertNotIn("PRIVATE", str(logs.output))

    def test_unknown_event_metric_and_invalid_values_are_not_exported(self):
        with self.assertRaises(ValueError):
            self.backend.event("arbitrary event", {})
        with self.assertRaises(ValueError):
            self.backend.metric("arbitrary metric", 1, {})
        with self.assertLogs("issuelens.telemetry.export", level="WARNING"):
            self.backend.metric("issuelens.run.duration", float("nan"), {})
        self.assertEqual(self.logs.get_finished_logs(), ())


if __name__ == "__main__":
    unittest.main()
