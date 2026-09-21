"""Use the hosting runtime's providers; never configure a second exporter."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry._logs import LogRecord, NoOpLogger, SeverityNumber, get_logger, get_logger_provider
from opentelemetry.context import Context, attach, detach
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.trace import Span, SpanKind

from telemetry import Attributes, SCHEMA_VERSION


logger = logging.getLogger("issuelens.telemetry.export")
EVENTS = frozenset({
    "issuelens.run.started", "issuelens.run.completed", "issuelens.run.agent",
    "issuelens.run.model", "issuelens.run.target", "issuelens.run.error",
    "issuelens.request.rejected",
})
HISTOGRAMS = {
    "issuelens.run.duration": "s",
    "issuelens.run.time_to_first_output": "s",
    "issuelens.host.duration": "s",
    "issuelens.model.time_to_first_token": "s",
    "gen_ai.client.token.usage": "{token}",
    "gen_ai.client.operation.duration": "s",
    "gen_ai.execute_tool.duration": "s",
    "gen_ai.invoke_agent.duration": "s",
}
COUNTERS = frozenset({
    "issuelens.model.retries", "issuelens.model.failures", "issuelens.telemetry.incomplete",
    "issuelens.run.count",
})
UP_DOWN_COUNTERS = frozenset({"issuelens.run.active"})


@contextmanager
def _isolated_context(parent: Span = trace.INVALID_SPAN) -> Iterator[Context]:
    # Hosting processors inspect ambient baggage, not just the record's context.
    context = trace.set_span_in_context(parent, Context())
    token = attach(context)
    try:
        yield context
    finally:
        detach(token)


class OpenTelemetryBackend:
    def __init__(self, *, tracer=None, meter=None, event_logger=None):
        self.tracer = tracer if tracer is not None else trace.get_tracer("issuelens", SCHEMA_VERSION)
        self.meter = meter if meter is not None else metrics.get_meter("issuelens", SCHEMA_VERSION)
        self.event_logger = event_logger if event_logger is not None else get_logger("issuelens", SCHEMA_VERSION)
        self.global_logger = event_logger is None
        self.instruments = {}
        self.failures = 0
        self.warned: set[str] = set()

    def _failure(self, operation: str) -> None:
        self.failures += 1
        if operation not in self.warned:
            self.warned.add(operation)
            with _isolated_context():
                logger.warning("IssueLens telemetry %s unavailable; measurements may be missing", operation)

    def event(self, name: str, attributes: Attributes) -> bool:
        if name not in EVENTS:
            raise ValueError("Unsupported telemetry event")
        if isinstance(self.event_logger, NoOpLogger) or (
            self.global_logger and not isinstance(get_logger_provider(), LoggerProvider)
        ):
            self._failure("logger provider")
            return False
        try:
            # BI facts are independent records, not sampled children of a trace.
            # Explicit run_trace_id retains correlation without inheriting flags.
            with _isolated_context() as context:
                self.event_logger.emit(LogRecord(
                    timestamp=time.time_ns(), context=context,
                    severity_number=SeverityNumber.INFO, severity_text="INFO",
                    body=name, attributes={"microsoft.custom_event.name": name, **attributes},
                ))
            return True
        except Exception:
            self._failure("event export")
            return False

    def metric(self, name: str, value: float, attributes: Attributes) -> None:
        if name not in HISTOGRAMS and name not in COUNTERS | UP_DOWN_COUNTERS:
            raise ValueError("Unsupported telemetry metric")
        if not math.isfinite(value) or (value < 0 and name not in UP_DOWN_COUNTERS):
            self._failure("metric value")
            return
        try:
            instrument = self.instruments.get(name)
            if instrument is None:
                if name in HISTOGRAMS:
                    instrument = self.meter.create_histogram(name, unit=HISTOGRAMS[name])
                elif name in UP_DOWN_COUNTERS:
                    instrument = self.meter.create_up_down_counter(name, unit="{run}")
                else:
                    instrument = self.meter.create_counter(name, unit="{event}")
                self.instruments[name] = instrument
            if name in HISTOGRAMS:
                instrument.record(value, attributes, context=trace.set_span_in_context(trace.INVALID_SPAN, Context()))
            else:
                instrument.add(value, attributes, context=trace.set_span_in_context(trace.INVALID_SPAN, Context()))
        except Exception:
            self._failure("metric export")

    def span(
        self, name: str, attributes: Attributes, *, parent: Span | None = None,
        start_ns: int | None = None,
    ) -> Span:
        try:
            if parent is None:
                # Preserve the host trace, not arbitrary host-span attributes.
                parent = trace.NonRecordingSpan(trace.get_current_span().get_span_context())
            with _isolated_context(parent) as context:
                return self.tracer.start_span(
                    name, attributes=attributes, start_time=start_ns, context=context,
                    kind=SpanKind.CLIENT if attributes.get("gen_ai.operation.name") == "chat" else SpanKind.INTERNAL,
                )
        except Exception:
            self._failure("span export")
            return trace.INVALID_SPAN
