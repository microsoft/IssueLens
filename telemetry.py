"""Content-free, request-scoped accounting of Copilot execution events."""

from __future__ import annotations

import logging
import math
import os
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol

from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode

from github_app_mcp.src.issuelens_github_mcp.auth import (
    GitHubAppError,
    validate_repository,
)
from telemetry_targets import result_metadata


logger = logging.getLogger("issuelens.telemetry")
SCHEMA_VERSION = "1"
ROLES = frozenset({"issuelens", "triage", "find-criticals", "plan", "team-memory"})
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens",
)
READ_TOOLS = frozenset({
    "get_repository", "list_issues", "get_issue", "list_issue_comments",
    "get_issue_comment", "list_issue_reactions", "search_issues", "list_labels",
    "get_file", "get_pull_request", "list_pull_request_files",
    "list_pull_request_commits", "list_pull_request_reviews",
    "list_pull_request_review_comments", "get_commit", "compare_commits",
    "list_repository_tree", "search_repository_content", "list_merged_pull_requests",
    "get_wiki_snapshot", "list_wiki_pages", "get_wiki_page", "search_wiki",
    "list_wiki_history", "get_wiki_diff",
})
WRITE_TOOLS = frozenset({
    "add_labels", "set_assignees", "add_issue_comment", "add_eyes_reaction",
    "write_wiki_pages",
})
LOCAL_TOOLS = frozenset({"task", "issuelens-config", "send-email", "send-teams-notification"})
Scalar = str | int | float | bool
Attributes = dict[str, Scalar]


class Backend(Protocol):
    def event(self, name: str, attributes: Attributes) -> bool: ...
    def metric(self, name: str, value: float, attributes: Attributes) -> None: ...
    def span(
        self, name: str, attributes: Attributes, *, parent: Span | None = None,
        start_ns: int | None = None,
    ) -> Span: ...


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        camel = re.sub(r"_([a-z])", lambda match: match[1].upper(), name)
        return value.get(name, value.get(camel, default))
    return getattr(value, name, default)


def _identifier(value: Any) -> str | None:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        return value
    return None


def _actor(event: Any) -> str:
    value = _get(event, "agent_id")
    return "root" if value is None else _identifier(value) or "unattributed"


def _count(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 2**53:
        return value
    return None


def _seconds(value: Any) -> float | None:
    if isinstance(value, timedelta):
        value = value.total_seconds()
    else:
        return None
    return value if math.isfinite(value) and 0 <= value <= 86400 else None


def _repository(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 140:
        return None
    try:
        return validate_repository(value).casefold()
    except GitHubAppError:
        return None


def _tool_name(data: Any) -> tuple[str, str]:
    server = _get(data, "mcp_server_name")
    name = _get(data, "mcp_tool_name")
    wire_name = _get(data, "tool_name", "")
    if server in {"github", "wiki-writer"} and name in READ_TOOLS | WRITE_TOOLS:
        return f"{server}-{name}", name
    for prefix in ("github-", "wiki-writer-"):
        if isinstance(wire_name, str) and wire_name.startswith(prefix):
            name = wire_name.removeprefix(prefix)
            if name in READ_TOOLS | WRITE_TOOLS:
                return wire_name, name
    return (wire_name, wire_name) if wire_name in LOCAL_TOOLS else ("other", "other")


@dataclass(frozen=True)
class Settings:
    release: str = "unknown"

    @classmethod
    def from_environment(cls) -> Settings:
        release = os.environ.get("ISSUELENS_RELEASE", "unknown")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", release):
            raise ValueError("ISSUELENS_RELEASE must be a bounded identifier")
        return cls(release=release)


def prepare_environment() -> Settings:
    """Run before constructing the hosting runtime's telemetry providers."""
    settings = Settings.from_environment()
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
    # httpx INFO records contain full URLs, including notification SAS queries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return settings


def copilot_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith(("OTEL_EXPORTER_OTLP", "COPILOT_OTEL_")):
            environment.pop(name)
    environment["COPILOT_OTEL_ENABLED"] = "false"
    environment["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
    return environment


@dataclass
class Usage:
    calls: int = 0
    totals: dict[str, int] = field(default_factory=dict)
    present: dict[str, int] = field(default_factory=dict)

    def add(self, data: Any) -> None:
        self.calls += 1
        for name in TOKEN_FIELDS:
            value = _count(_get(data, name))
            if value is not None:
                self.totals[name] = self.totals.get(name, 0) + value
                self.present[name] = self.present.get(name, 0) + 1

    def attributes(self) -> Attributes:
        result: Attributes = {"usage_calls": self.calls}
        result.update(self.totals)
        result.update({f"{name}_calls": count for name, count in self.present.items()})
        return result


@dataclass
class Agent:
    identity: str
    role: str
    span: Span
    started: float
    parent: str = ""
    finished: bool = False
    usage: Usage = field(default_factory=Usage)
    tools_started: int = 0
    tools_completed: int = 0
    tools_failed: int = 0
    status: str = "incomplete"
    duration_s: float = 0.0


@dataclass
class ToolCall:
    name: str
    operation: str
    owner: Agent
    span: Span
    started: float
    repository: str | None
    number: int | None
    kind: str
    finished: bool = False


class RunTelemetry:
    MAX_EVENTS = 8192
    MAX_TOOLS = 1024
    MAX_AGENTS = 128
    MAX_TARGETS = 512
    MAX_MESSAGES = 512
    MAX_MODELS = 16

    def __init__(
        self, backend: Backend, settings: Settings, protocol: str, *,
        identifiers: Mapping[str, Any] | None = None,
        clock=time.monotonic, wall_clock=time.time_ns,
    ):
        if protocol not in {"invocations", "responses"}:
            raise ValueError("Unsupported telemetry protocol")
        self.backend = backend
        self.settings = settings
        self.protocol = protocol
        self.clock = clock
        self.wall_clock = wall_clock
        self.started = clock()
        self.started_ns = wall_clock()
        self.run_id = str(uuid.uuid4())
        self.finished = False
        self.admitted = False
        self.model_sent = False
        self.session_failed = False
        self.first_output: float | None = None
        self.first_final_output: float | None = None
        self.usage = Usage()
        self.models: dict[str, Usage] = {}
        self.seen: set[tuple[str, str, str]] = set()
        self.calls: set[tuple[str, str]] = set()
        self.tools: dict[tuple[str, str], ToolCall] = {}
        self.agents: dict[str, Agent] = {}
        self.delegations: dict[str, list[Agent]] = {}
        self.ambiguous_delegations: set[str] = set()
        self.phases: dict[tuple[str, str], str] = {}
        self.targets: dict[tuple[str, str, int, str], int] = {}
        self.item_types: dict[tuple[str, int], str] = {}
        self.repository_ids: dict[str, int] = {}
        self.quality: dict[str, int] = {}
        self.model_failures = 0
        self.retries = 0
        self.write_operations = 0
        self.notification_submissions = 0
        self.context_tokens_peak = 0
        self.context_token_limit = 0
        self.context_usage_observed = False
        self.identifiers: Attributes = {}
        for key, value in (identifiers or {}).items():
            if key in {"invocation_id", "response_id", "conversation_id", "session_id"}:
                self.set_identifier(key, value)
        self.span = backend.span(
            "invoke_agent issuelens", self._span_attributes("invoke_agent"), start_ns=self.started_ns,
        )
        context = self.span.get_span_context()
        self.trace_id = format(context.trace_id, "032x") if context.is_valid else ""
        self.root = Agent("root", "issuelens", self.span, self.started)
        self.agents["root"] = self.root

    def set_identifier(self, name: str, value: Any) -> None:
        if name not in {"invocation_id", "response_id", "conversation_id", "session_id"}:
            raise ValueError("Unsupported telemetry identifier")
        if (identifier := _identifier(value)) is not None:
            self.identifiers[name] = identifier

    def _common(self) -> Attributes:
        return {
            "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
            "protocol": self.protocol,
            "release": self.settings.release, "run_trace_id": self.trace_id,
            **self.identifiers,
        }

    def _span_attributes(self, operation: str) -> Attributes:
        attributes: Attributes = {
            "gen_ai.operation.name": operation, "issuelens.run_id": self.run_id,
            "issuelens.protocol": self.protocol, "issuelens.agent.role": "issuelens",
            "issuelens.schema_version": SCHEMA_VERSION,
        }
        if operation == "chat":
            attributes["gen_ai.provider.name"] = self._provider()
        return attributes

    @staticmethod
    def _provider() -> str:
        return "azure.ai.openai" if (
            os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
            and os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
        ) else "github"

    def _event(self, name: str, attributes: Attributes) -> None:
        if not self.backend.event(name, {**self._common(), **attributes}):
            self.quality["export_rejected"] = self.quality.get("export_rejected", 0) + 1

    def _metric(self, name: str, value: float, **attributes: Scalar) -> None:
        if name.startswith("gen_ai."):
            if "model" in attributes:
                attributes["gen_ai.request.model"] = attributes.pop("model")
                attributes["gen_ai.provider.name"] = self._provider()
            for short, semantic in (("token_type", "gen_ai.token.type"), ("tool", "gen_ai.tool.name")):
                if short in attributes:
                    attributes[semantic] = attributes.pop(short)
            if "role" in attributes:
                attributes["gen_ai.agent.name"] = attributes.pop("role")
            attributes["gen_ai.operation.name"] = (
                "chat" if name.startswith("gen_ai.client.") else
                "execute_tool" if name.startswith("gen_ai.execute_tool.") else "invoke_agent"
            )
        self.backend.metric(name, value, {
            "protocol": self.protocol,
            **attributes,
        })

    def _lost(self, reason: str) -> None:
        self.quality[reason] = self.quality.get(reason, 0) + 1
        self._metric("issuelens.telemetry.incomplete", 1, reason=reason)

    def degraded(self, reason: str) -> None:
        if reason not in {"media_unavailable", "resume_fallback", "cleanup_failed"}:
            raise ValueError("Unsupported telemetry degradation")
        self._lost(reason)

    def host_issue_read(self, repository: str, number: int) -> None:
        """Observe a completed trusted host read, not a prompt reference."""
        repo = _repository(repository)
        if not self.finished and repo and _count(number):
            self._target(repo, self.item_types.get((repo, number), "work_item"), number, "read")

    @contextmanager
    def activate(self) -> Iterator[None]:
        with trace.use_span(self.span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            yield

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if name not in {"client_start", "media_load", "session_open", "session_send", "session_close"}:
            raise ValueError("Unsupported telemetry phase")
        started = self.clock()
        span = self.backend.span(f"issuelens.{name}", {
            **self._span_attributes("issuelens.host"), "issuelens.phase": name,
        }, parent=self.span, start_ns=self.wall_clock())
        try:
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                yield
        except BaseException:
            span.set_status(StatusCode.ERROR)
            raise
        finally:
            span.end(end_time=self.wall_clock())
            self._metric("issuelens.host.duration", self.clock() - started, phase=name)

    def admit(self) -> None:
        if self.admitted or self.finished:
            return
        self.admitted = True
        packages = {}
        for package, name in (
            ("github-copilot-sdk", "sdk_version"),
            ("azure-ai-agentserver-core", "host_version"),
        ):
            try:
                packages[name] = version(package)
            except PackageNotFoundError:
                packages[name] = "unknown"
        self._event("issuelens.run.started", packages)
        self._metric("issuelens.run.active", 1)

    def observe(self, event: Any) -> None:
        if self.finished:
            return
        try:
            self._observe(event)
        except Exception:
            # Telemetry cannot fail a model turn or cause a business retry.
            self._lost("invalid_event")
            if self.quality["invalid_event"] == 1:
                logger.warning("IssueLens telemetry rejected an event; accounting is incomplete")

    def _observe(self, event: Any) -> None:
        kind = _get(event, "type")
        kind = getattr(kind, "value", kind)
        data = _get(event, "data")
        actor = _actor(event)
        if kind == "assistant.message_start":
            message_id = _identifier(_get(data, "message_id"))
            if message_id is not None:
                key = (actor, message_id)
                if key in self.phases or len(self.phases) < self.MAX_MESSAGES:
                    phase = _get(data, "phase")
                    if self.phases.get(key) not in {"analysis", "reasoning"}:
                        self.phases[key] = phase if phase in {"analysis", "reasoning", "final", "commentary"} else "unknown"
                else:
                    self._lost("message_limit")
            return
        if kind == "session.usage_info":
            current, limit = _count(_get(data, "current_tokens")), _count(_get(data, "token_limit"))
            if current is not None and limit is not None:
                self.context_tokens_peak = max(self.context_tokens_peak, current)
                self.context_token_limit = limit
                self.context_usage_observed = True
            return
        supported = {
            "assistant.usage", "tool.execution_start", "tool.execution_complete",
            "subagent.started", "subagent.completed", "subagent.failed",
            "assistant.turn_retry", "model.call_failure", "session.error",
        }
        if kind not in supported:
            return
        identity = _identifier(_get(event, "id"))
        if identity is None:
            self._lost("missing_event_id")
            return
        event_key = (actor, _identifier(_get(data, "parent_tool_call_id")) or "", identity)
        if event_key in self.seen:
            return
        if len(self.seen) >= self.MAX_EVENTS:
            self._lost("event_limit")
            return
        self.seen.add(event_key)
        if actor == "unattributed":
            self._lost("invalid_agent_id")
        if kind.startswith("subagent."):
            self._subagent(kind, actor, data)
        elif kind == "assistant.usage":
            self._usage(actor, data)
        elif kind.startswith("tool.execution_"):
            self._tool(kind, actor, data)
        elif kind == "assistant.turn_retry":
            self.retries += 1
            self._metric("issuelens.model.retries", 1)
        elif kind == "model.call_failure":
            self.model_failures += 1
            self._metric("issuelens.model.failures", 1, error_type=self._error_code(data))
            self._event("issuelens.run.error", {"stage": "model", "error_type": self._error_code(data)})
            owner = self._owner(actor, data)
            duration = _seconds(_get(data, "duration"))
            if duration is not None:
                end_ns = self.wall_clock()
                span = self.backend.span("chat", {
                    **self._span_attributes("chat"),
                    "issuelens.agent.role": owner.role,
                    "gen_ai.request.model": _identifier(_get(data, "model")) or "unknown",
                    "error.type": self._error_code(data),
                }, parent=owner.span, start_ns=max(self.started_ns, end_ns - int(duration * 1e9)))
                span.set_status(StatusCode.ERROR)
                span.end(end_time=end_ns)
        else:
            self.session_failed = True
            self._event("issuelens.run.error", {"stage": "session", "error_type": self._error_code(data)})

    @staticmethod
    def _error_code(data: Any) -> str:
        code = _get(data, "status_code")
        if code in {401, 403}:
            return "authentication"
        if code == 429:
            return "rate_limit"
        if code in {408, 504}:
            return "timeout"
        return "execution_error"

    def visible_output(self, event: Any) -> None:
        """Called only when the protocol is about to emit this assistant text."""
        if self.finished:
            return
        data = _get(event, "data")
        actor = _actor(event)
        if actor != "root" or _get(data, "parent_tool_call_id"):
            return
        phase = self.phases.get((actor, _identifier(_get(data, "message_id")) or ""))
        if phase is None or phase in {"analysis", "reasoning"}:
            return
        content = _get(data, "delta_content", _get(data, "content"))
        if not isinstance(content, str) or not content.strip() or _get(data, "tool_requests"):
            return
        elapsed = max(0, self.clock() - self.started)
        if self.first_output is None:
            self.first_output = elapsed
        if phase == "final" and self.first_final_output is None:
            self.first_final_output = elapsed

    def _owner(self, actor: str, data: Any) -> Agent:
        if actor == "unattributed":
            return self._unattributed()
        if actor != "root" and actor in self.agents:
            return self.agents[actor]
        legacy_parent = _identifier(_get(data, "parent_tool_call_id"))
        candidates = self.delegations.get(legacy_parent or "", [])
        if legacy_parent:
            if len(candidates) == 1 and legacy_parent not in self.ambiguous_delegations:
                if actor != "root" and len(self.agents) < self.MAX_AGENTS:
                    self.agents[actor] = candidates[0]
                return candidates[0]
            if actor == "root":
                self._lost("ambiguous_delegation" if candidates else "missing_agent_start")
                return self._unattributed()
        if actor not in self.agents:
            if len(self.agents) < self.MAX_AGENTS:
                self.agents[actor] = Agent(
                    actor, "unknown", self.backend.span("invoke_agent unknown", {
                        **self._span_attributes("invoke_agent"), "issuelens.agent.role": "unknown",
                    }, parent=self.span, start_ns=self.wall_clock()), self.clock(),
                )
            else:
                self._lost("agent_limit")
                return self._unattributed()
        return self.agents[actor]

    def _unattributed(self) -> Agent:
        if "unattributed" not in self.agents:
            self.agents["unattributed"] = Agent(
                "unattributed", "unknown", trace.INVALID_SPAN, self.clock(),
            )
        return self.agents["unattributed"]

    def _subagent(self, kind: str, actor: str, data: Any) -> None:
        call_id = _identifier(_get(data, "tool_call_id"))
        if call_id is None:
            self._lost("missing_tool_id")
            return
        candidates = self.delegations.get(call_id, [])
        actor_agent = self.agents.get(actor)
        if kind == "subagent.started":
            if actor == "unattributed":
                return
            parents = [tool for (_, identifier), tool in self.tools.items()
                       if identifier == call_id and not tool.finished]
            scoped = [tool for tool in parents if tool.owner is actor_agent]
            if len(scoped) == 1:
                parents = scoped
            if len(parents) > 1:
                self._lost("ambiguous_delegation")
                self.ambiguous_delegations.add(call_id)
                return
            parent = parents[0].owner if parents else self.root
            if actor_agent in candidates or (
                actor_agent is parent and any(agent.parent == parent.identity for agent in candidates)
            ):
                return
            if len(self.agents) >= self.MAX_AGENTS:
                self._lost("agent_limit")
                return
            parent_known = bool(parents) or actor == "root"
            if not parent_known:
                self._lost("missing_agent_parent")
            role = _get(data, "agent_name")
            role = role if role in ROLES else "unknown"
            identity = actor if actor_agent is not parent and actor != "root" else f"delegation:{call_id}"
            if identity.startswith("delegation:") and identity in self.agents:
                identity = f"delegation:{uuid.uuid4()}"
            agent = self.agents.get(identity)
            if agent is None:
                parent_id = parent.identity if parent_known else ""
                agent = Agent(identity, role, self.backend.span(f"invoke_agent {role}", {
                    **self._span_attributes("invoke_agent"), "issuelens.agent.role": role,
                    "issuelens.agent_run_id": identity, "issuelens.parent_agent_id": parent_id,
                }, parent=parents[0].span if parents else self.span, start_ns=self.wall_clock()),
                    self.clock(), parent_id)
                self.agents[identity] = agent
            else:
                self._lost("late_agent_start")
                agent.role = role
                agent.parent = parent.identity if parent_known else ""
                agent.span.set_attribute("issuelens.agent.role", role)
                agent.span.set_attribute("issuelens.parent_agent_id", agent.parent)
            self.delegations.setdefault(call_id, []).append(agent)
        else:
            scoped = [agent for agent in candidates if agent is actor_agent]
            if not scoped and actor_agent is not None:
                scoped = [agent for agent in candidates if agent.parent == actor_agent.identity]
            if not scoped and call_id in self.ambiguous_delegations:
                self._lost("ambiguous_delegation")
                return
            candidates = scoped or candidates
            if len(candidates) != 1:
                self._lost("ambiguous_delegation" if candidates else "missing_agent_start")
                return
            agent = candidates[0]
            if not agent.finished:
                agent.status = "failed" if kind == "subagent.failed" else "completed"
                self._end_agent(agent)

    def _usage(self, actor: str, data: Any) -> None:
        owner = self._owner(actor, data)
        call_id = _identifier(_get(data, "api_call_id"))
        if call_id is not None:
            key = (owner.identity, call_id)
            if key in self.calls:
                return
            self.calls.add(key)
        model = _identifier(_get(data, "model")) or "unknown"
        if model not in self.models and len(self.models) >= self.MAX_MODELS:
            model = "other"
            self._lost("model_limit")
        self.usage.add(data)
        owner.usage.add(data)
        self.models.setdefault(model, Usage()).add(data)
        attributes = {
            **self._span_attributes("chat"), "issuelens.agent.role": owner.role,
            "gen_ai.request.model": model,
        }
        token_attributes = {
            "input_tokens": "gen_ai.usage.input_tokens",
            "output_tokens": "gen_ai.usage.output_tokens",
            "cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
            "cache_write_tokens": "gen_ai.usage.cache_write.input_tokens",
            "reasoning_tokens": "gen_ai.usage.reasoning.output_tokens",
        }
        for name, attribute in token_attributes.items():
            if (value := _count(_get(data, name))) is not None:
                attributes[attribute] = value
                if name in {"input_tokens", "output_tokens"}:
                    self._metric("gen_ai.client.token.usage", value, model=model,
                                 role=owner.role, token_type=name.removesuffix("_tokens"))
        duration = _seconds(_get(data, "duration"))
        if duration is not None:
            end_ns = self.wall_clock()
            span = self.backend.span("chat", attributes, parent=owner.span,
                                     start_ns=max(self.started_ns, end_ns - int(duration * 1e9)))
            span.end(end_time=end_ns)
            self._metric("gen_ai.client.operation.duration", duration, model=model, role=owner.role)
        if (ttft := _seconds(_get(data, "time_to_first_token"))) is not None:
            self._metric("issuelens.model.time_to_first_token", ttft, model=model, role=owner.role)

    def _tool(self, kind: str, actor: str, data: Any) -> None:
        identifier = _identifier(_get(data, "tool_call_id"))
        if identifier is None:
            self._lost("missing_tool_id")
            return
        owner = self._owner(actor, data)
        key = (owner.identity, identifier)
        if kind == "tool.execution_start":
            if key in self.tools:
                return
            if len(self.tools) >= self.MAX_TOOLS:
                self._lost("tool_limit")
                return
            name, operation = _tool_name(data)
            args = _get(data, "arguments")
            repo = _repository(_get(args, "repository")) if operation in READ_TOOLS | WRITE_TOOLS | {"issuelens-config"} else None
            number = _count(_get(args, "issue_number")) or _count(_get(args, "pull_number"))
            target_kind = "pull_request" if _get(args, "pull_number") and operation in READ_TOOLS else "work_item"
            if repo and number:
                target_kind = self.item_types.get((repo, number), target_kind)
            if repo:
                self._target(repo, target_kind if number else "repository", number or 0, "attempted")
            span = self.backend.span(f"execute_tool {name}", {
                **self._span_attributes("execute_tool"), "issuelens.agent.role": owner.role,
                "gen_ai.tool.name": name, "gen_ai.tool.call.id": identifier,
            }, parent=owner.span, start_ns=self.wall_clock())
            self.tools[key] = ToolCall(name, operation, owner, span, self.clock(), repo, number, target_kind)
            owner.tools_started += 1
            return
        tool = self.tools.get(key)
        if tool is None:
            self._lost("missing_tool_start")
            return
        if tool.finished:
            return
        tool.finished = True
        metadata = result_metadata(_get(data, "result"))
        success = _get(data, "success") is True and not metadata.get("is_error")
        tool.owner.tools_completed += 1
        tool.owner.tools_failed += int(not success)
        if not success:
            tool.span.set_status(StatusCode.ERROR)
            tool.span.set_attribute("error.type", "tool_error")
            self._event("issuelens.run.error", {
                "stage": "tool", "error_type": "tool_error", "tool": tool.name,
                "role": tool.owner.role, "agent_run_id": tool.owner.identity,
            })
        tool.span.end(end_time=self.wall_clock())
        self._metric("gen_ai.execute_tool.duration", self.clock() - tool.started,
                     tool=tool.name, role=tool.owner.role, status="completed" if success else "failed")
        if success:
            self._tool_targets(tool, metadata)

    def _target(self, repo: str, kind: str, number: int, relationship: str) -> None:
        key = (repo, kind, number, relationship)
        if key not in self.targets and len(self.targets) >= self.MAX_TARGETS:
            self._lost("target_limit")
            return
        self.targets[key] = self.targets.get(key, 0) + 1

    def _tool_targets(self, tool: ToolCall, metadata: dict[str, Any]) -> None:
        if tool.operation in {"send-email", "send-teams-notification"}:
            self.notification_submissions += 1
        repo = tool.repository
        if repo is None:
            return
        kind = tool.kind
        if tool.operation == "get_repository" and _repository(metadata.get("full_name")) == repo:
            if (repository_id := _count(metadata.get("id"))) is not None:
                self.repository_ids[repo] = repository_id
        if tool.operation in {"get_issue", "get_pull_request"} and metadata.get("number") == tool.number:
            kind = "pull_request" if tool.operation == "get_pull_request" or metadata.get("is_pull_request") else "issue"
            self.item_types[(repo, tool.number)] = kind
            for key in tuple(self.targets):
                if key[:3] == (repo, "work_item", tool.number):
                    count = self.targets.pop(key)
                    resolved = (repo, kind, tool.number, key[3])
                    self.targets[resolved] = self.targets.get(resolved, 0) + count
        if "wiki" in tool.operation:
            destination = _repository(metadata.get("wiki_repository"))
            source = _repository(metadata.get("source_repository"))
            if destination and source == repo:
                self._target(repo, "repository", 0, "source")
                relationship = "read"
                if tool.operation == "write_wiki_pages":
                    if metadata.get("status") not in {"updated", "no-change"}:
                        self._lost("unconfirmed_wiki_write")
                        return
                    relationship = "write_succeeded"
                    self.write_operations += 1
                self._target(destination, "wiki", 0, relationship)
            else:
                self._lost("missing_wiki_identity")
            return
        relationship = "write_succeeded" if tool.operation in WRITE_TOOLS else "read"
        if tool.operation in WRITE_TOOLS:
            self.write_operations += 1
        self._target(repo, kind if tool.number else "repository", tool.number or 0, relationship)

    def _end_agent(self, agent: Agent) -> None:
        if agent.finished:
            return
        agent.finished = True
        agent.duration_s = max(0, self.clock() - agent.started)
        if agent.status != "completed":
            agent.span.set_status(StatusCode.ERROR)
            agent.span.set_attribute("error.type", "agent_incomplete" if agent.status == "incomplete" else "agent_error")
        if agent is not self.root:
            agent.span.end(end_time=self.wall_clock())
            self._metric("gen_ai.invoke_agent.duration", agent.duration_s,
                         role=agent.role, status=agent.status)
            if agent.status != "completed":
                self._event("issuelens.run.error", {
                    "stage": "agent", "error_type": "agent_incomplete" if agent.status == "incomplete" else "agent_error",
                    "role": agent.role, "agent_run_id": agent.identity,
                })

    def finish(
        self, status: str, *, stage: str = "session", error_type: str = "",
        no_action: bool = False, transport_status: str = "unknown",
    ) -> None:
        if self.finished:
            return
        if status not in {"completed", "failed", "cancelled", "rejected"}:
            raise ValueError("Unsupported telemetry status")
        if stage not in {"validation", "setup", "session", "stream", "cleanup"}:
            raise ValueError("Unsupported telemetry stage")
        if error_type not in {"", "invalid_input", "configuration", "timeout", "cancelled",
                              "stream_interrupted", "execution_error", "concurrent_turn"}:
            raise ValueError("Unsupported telemetry error")
        if transport_status not in {"completed", "interrupted", "rejected", "unknown"}:
            raise ValueError("Unsupported transport status")
        self.finished = True
        if self.session_failed and status == "completed":
            status = "failed"
            error_type = "execution_error"
        self.root.status = status
        for tool in self.tools.values():
            if not tool.finished:
                tool.span.set_attribute("issuelens.incomplete", True)
                tool.span.set_status(StatusCode.ERROR)
                tool.span.end(end_time=self.wall_clock())
                self._lost("unfinished_tool")
        agents = {agent.identity: agent for agent in self.agents.values()}
        for agent in agents.values():
            if not agent.finished:
                if agent is not self.root:
                    self._lost("unfinished_agent")
                self._end_agent(agent)
        attribution_complete = not any(agent.role == "unknown" for agent in agents.values())
        if not attribution_complete:
            self._lost("unknown_agent")
        for agent in agents.values():
            self._event("issuelens.run.agent", {
                "agent_run_id": agent.identity, "role": agent.role,
                "parent_agent_id": agent.parent, "status": agent.status,
                "duration_s": agent.duration_s,
                "tools_started": agent.tools_started, "tools_completed": agent.tools_completed,
                "tools_failed": agent.tools_failed, **agent.usage.attributes(),
            })
        for model, usage in self.models.items():
            self._event("issuelens.run.model", {"model": model, **usage.attributes()})
        for (repo, kind, number, relationship), count in self.targets.items():
            attributes: Attributes = {
                "repository": repo, "target_kind": kind, "number": number,
                "relationship": relationship, "operations": count,
            }
            if repo in self.repository_ids:
                attributes["repository_id"] = self.repository_ids[repo]
            self._event("issuelens.run.target", attributes)
        duration = max(0, self.clock() - self.started)
        usage_status = "not_applicable" if not self.model_sent else "unavailable"
        if self.usage.calls:
            usage_status = "complete" if all(
                self.usage.present.get(name) == self.usage.calls for name in ("input_tokens", "output_tokens")
            ) and not self.model_failures and not any(
                reason in self.quality for reason in {"invalid_event", "event_limit", "missing_event_id"}
            ) else "partial"
        effects = self.write_operations + self.notification_submissions
        outcome = "no_action" if no_action else "unknown"
        if effects:
            outcome = "confirmed_operations" if status == "completed" else "partial"
        attributes: Attributes = {
            "execution_status": status, "business_outcome": outcome, "admitted": self.admitted,
            "transport_status": transport_status,
            "stage": stage, "error_type": error_type, "duration_s": duration,
            "usage_status": usage_status, "attribution_complete": attribution_complete,
            "model_request_sent": self.model_sent,
            "model_failures": self.model_failures, "model_retries": self.retries,
            "tools_started": sum(agent.tools_started for agent in agents.values()),
            "tools_completed": sum(agent.tools_completed for agent in agents.values()),
            "tools_failed": sum(agent.tools_failed for agent in agents.values()),
            "agents_started": max(0, len(agents) - 1),
            "write_operations_succeeded": self.write_operations,
            "notification_submissions": self.notification_submissions,
            "telemetry_incomplete": bool(self.quality),
            **self.usage.attributes(),
            **{f"incomplete_{reason}": count for reason, count in self.quality.items()},
        }
        if self.first_output is not None:
            attributes["first_root_output_s"] = self.first_output
            self._metric("issuelens.run.time_to_first_output", self.first_output)
        if self.first_final_output is not None:
            attributes["first_final_output_s"] = self.first_final_output
        if self.context_usage_observed:
            attributes.update(context_tokens_peak=self.context_tokens_peak, context_token_limit=self.context_token_limit)
        self._event("issuelens.run.completed" if self.admitted else "issuelens.request.rejected", attributes)
        self.span.set_attribute("issuelens.execution_status", status)
        self.span.set_attribute("issuelens.business_outcome", outcome)
        if status != "completed":
            self.span.set_status(StatusCode.ERROR)
        self.span.end(end_time=self.wall_clock())
        self._metric("issuelens.run.duration", duration, status=status, admitted=self.admitted)
        self._metric("issuelens.run.count", 1, status=status, admitted=self.admitted)
        if self.admitted:
            self._metric("issuelens.run.active", -1)
