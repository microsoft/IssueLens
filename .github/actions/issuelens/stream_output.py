"""Bounded presentation of IssueLens events; never decides invocation success."""

import html
import re
import sys
import time
import unicodedata


def normalize_terminal(text, state):
    output = []
    escape = state.get("escape", "")
    for char in text:
        if escape == "escape":
            escape = "csi" if char == "[" else "osc" if char == "]" else ""
        elif escape == "csi":
            if "@" <= char <= "~":
                escape = ""
        elif escape in {"osc", "osc-escape"}:
            if char == "\x07" or (escape == "osc-escape" and char == "\\"):
                escape = ""
            else:
                escape = "osc-escape" if char == "\x1b" else "osc"
        elif char == "\x1b":
            escape = "escape"
        elif char == "\r":
            output.append("\n")
        elif char in "\n\t" or unicodedata.category(char)[0] != "C":
            output.append(char)
    state["escape"] = escape
    return "".join(output)


def safe_text(text, secrets=()):
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    text = normalize_terminal(text, {})
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text.replace("::", ": :")


def message_key(event, data):
    identity = event.get("agentId") or data.get("parentToolCallId")
    scope = identity if isinstance(identity, str) and len(identity) <= 256 else "root"
    message_id = data.get("messageId")
    if not isinstance(message_id, str) or len(message_id) > 256:
        message_id = "unidentified"
    return scope, message_id


class MessagePhases:
    MAX_ITEMS = 256

    def __init__(self):
        self.blocked = {}
        self.limited = False

    def allows(self, event, data):
        key = message_key(event, data)
        if key not in self.blocked and len(self.blocked) >= self.MAX_ITEMS:
            self.limited = True
            return False
        blocked = self.blocked.get(key, False) or data.get("phase") in ("analysis", "reasoning")
        self.blocked[key] = blocked
        return not blocked


class StreamRenderer:
    MAX_ITEMS = 256
    MAX_MESSAGE = 64 * 1024
    MAX_TEXT = 512 * 1024
    MAX_LOG = 256 * 1024
    MAX_SUMMARY = 256 * 1024
    CHUNK = 1024
    ROLES = {"issuelens", "triage", "plan", "find-criticals", "team-memory"}

    def __init__(self, mode="hybrid", stream=None, clock=None, secrets=()):
        if mode not in {"hybrid", "activity", "quiet"}:
            raise ValueError("output-mode must be hybrid, activity, or quiet")
        self.mode = mode
        self.stream = stream if stream is not None else sys.stdout
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.secrets = tuple(secret for secret in secrets if secret)
        self.holdback = max((len(secret) - 1 for secret in self.secrets), default=0)
        self.messages = {}
        self.phases = MessagePhases()
        self.tools = {}
        self.agents = {}
        self.seen = set()
        self.text_size = 0
        self.log_size = 0
        self.limited = False
        self.finished = False
        self.tool_count = 0
        self.failed_tools = 0
        self.retries = 0
        self.retry_pending = False
        self.display_truncated = False
        self.io_failed = False

    @property
    def elapsed(self):
        return max(0, self.clock() - self.started)

    def _line(self, label, text):
        if self.mode == "quiet" or self.limited:
            return
        for line in safe_text(str(text), self.secrets).splitlines():
            rendered = f"[{self.elapsed:6.1f}s] [{label}] {line}\n"
            size = len(rendered.encode("utf-8"))
            if self.log_size + size > self.MAX_LOG:
                self.limited = True
                self.display_truncated = True
                self._emit("[IssueLens] Live display limit reached; response validation continues.\n")
                return
            self.log_size += size
            if not self._emit(rendered):
                return

    def _emit(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            self.io_failed = True
            self.mode = "quiet"
            return False
        return True

    def _scope(self, event, data):
        return message_key(event, data)[0]

    def _label(self, scope):
        return "IssueLens" if scope == "root" else self.agents.get(scope, "sub-agent")

    def _flush(self, state, force=False):
        pending = state["pending"]
        while pending:
            newline = pending.find("\n")
            if newline >= 0:
                cut = newline + 1
            elif force:
                cut = len(pending)
            elif len(pending) > self.CHUNK + self.holdback or self.clock() - state["flushed"] >= 1:
                cut = min(self.CHUNK, max(0, len(pending) - self.holdback))
            else:
                break
            if not cut:
                break
            cut = min(cut, self.CHUNK)
            for secret in self.secrets:
                start = pending.find(secret, max(0, cut - len(secret) + 1))
                if 0 <= start < cut < start + len(secret):
                    cut = start + len(secret)
            self._line(state["label"], pending[:cut].rstrip("\n"))
            pending = pending[cut:]
            state["flushed"] = self.clock()
        state["pending"] = pending

    def tick(self):
        if self.mode == "hybrid" and not self.finished:
            for state in self.messages.values():
                self._flush(state)

    def _message(self, event, data, delta):
        if self.mode != "hybrid" or data.get("toolRequests"):
            return
        content = data.get("deltaContent" if delta else "content")
        if not isinstance(content, str):
            return
        scope, message_id = message_key(event, data)
        key = (scope, message_id)
        state = self.messages.get(key)
        if state is not None and state["done"]:
            if message_id != "unidentified" or state["text"] == content:
                return
            state = None
        if state is None:
            if len(self.messages) >= self.MAX_ITEMS:
                self.display_truncated = True
                return
            state = {"text": "", "pending": "", "done": False,
                     "label": self._label(scope), "flushed": self.clock()}
            self.messages[key] = state
        if delta:
            addition = content
        elif content.startswith(state["text"]):
            addition = content[len(state["text"]):]
        else:
            self._flush(state, force=True)
            self._line("warning", "Streamed text was revised; consult the final summary.")
            state["done"] = True
            return
        available = min(self.MAX_MESSAGE - len(state["text"]), self.MAX_TEXT - self.text_size)
        if len(addition) > available:
            self.display_truncated = True
        addition = addition[:max(0, available)]
        self.text_size += len(addition)
        state["text"] += addition
        state["pending"] += normalize_terminal(addition, state)
        if self.retry_pending and addition:
            self._line("activity", "Assistant output resumed after retry.")
            self.retry_pending = False
        self._flush(state, force=not delta)
        state["done"] = not delta

    def event(self, event):
        if self.finished or not isinstance(event, dict):
            return
        identifier = event.get("id")
        if isinstance(identifier, str) and len(identifier) <= 256:
            if identifier in self.seen:
                return
            if len(self.seen) < 4096:
                self.seen.add(identifier)
        data = event.get("data")
        if not isinstance(data, dict):
            return
        kind = event.get("type")
        scope = self._scope(event, data)
        if kind in {"assistant.message_start", "assistant.message_delta", "assistant.message"}:
            if not self.phases.allows(event, data):
                self.messages.pop(message_key(event, data), None)
            elif kind != "assistant.message_start":
                self._message(event, data, kind == "assistant.message_delta")
            self.display_truncated |= self.phases.limited
        elif kind in {"subagent.started", "subagent.completed", "subagent.failed"}:
            name = data.get("agentName")
            label = name if isinstance(name, str) and name in self.ROLES else "sub-agent"
            if kind == "subagent.started":
                for identifier in (data.get("toolCallId"), event.get("agentId"), data.get("agentId")):
                    if isinstance(identifier, str) and len(identifier) <= 256 and len(self.agents) < self.MAX_ITEMS:
                        self.agents[identifier] = label
            self._line(label, {"subagent.started": "Started.", "subagent.completed": "Finished.",
                               "subagent.failed": "Failed; awaiting the root agent result."}[kind])
        elif kind in {"tool.execution_start", "tool.execution_complete"}:
            identifier = data.get("toolCallId")
            if not isinstance(identifier, str) or len(identifier) > 256:
                return
            key = (scope, identifier)
            if kind == "tool.execution_start":
                if key in self.tools:
                    return
                if len(self.tools) >= self.MAX_ITEMS:
                    self.display_truncated = True
                    return
                name = data.get("toolName")
                name = name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) else "tool"
                self.tools[key] = {"name": name, "started": self.clock(), "done": False, "label": self._label(scope)}
                self.tool_count += 1
                self._line("tool", f"{self._label(scope)} / {name} started")
            else:
                state = self.tools.get(key)
                if state is None or state["done"]:
                    return
                state["done"] = True
                success = data.get("success")
                status = "completed" if success is True else "failed" if success is False else "finished (status unknown)"
                self.failed_tools += int(success is False)
                self._line("tool", f"{state['label']} / {state['name']} {status} ({max(0, self.clock() - state['started']):.1f}s)")
        elif kind == "model.call_failure":
            self._line("warning", "Model request failed; awaiting retry or termination.")
        elif kind == "assistant.turn_retry":
            self.retries += 1
            self.retry_pending = True
            self._line("warning", "Retrying model request.")
        self.tick()

    def finish(self, status):
        if self.finished:
            return
        if status not in {"completed", "updated", "no-change", "failed"}:
            raise ValueError("Invalid display status")
        for state in self.messages.values():
            self._flush(state, force=True)
        if status == "failed":
            self._line("error", "Invocation failed or its outcome is unknown. Displayed text may be incomplete; inspect the target before retrying.")
        else:
            self._line("IssueLens", f"Invocation {status}. Stream completion alone does not confirm requested writes.")
        if self.display_truncated and not self.limited:
            self._line("warning", "Live display was truncated; validation used the complete bounded response.")
        self.finished = True

    def summary(self, status, mode="full", text=None, wiki=None):
        if mode not in {"full", "status", "none"}:
            raise ValueError("summary-mode must be full, status, or none")
        if mode == "none":
            return ""
        headings = {"completed": "Invocation completed", "updated": "Team memory updated",
                    "no-change": "Team memory unchanged", "failed": "Invocation failed or outcome unknown"}
        report = (
            f"## IssueLens: {headings[status]}\n\n"
            f"| Elapsed | Tool calls observed | Failed tools observed | Model retries |\n"
            f"| --- | ---: | ---: | ---: |\n"
            f"| {self.elapsed:.1f}s | {self.tool_count} | {self.failed_tools} | {self.retries} |\n\n"
        )
        if status == "failed":
            report += "The response did not pass completion/result validation. Partial live text is not a confirmed result. Inspect the target before retrying; a write may already have occurred.\n"
        elif status == "completed":
            report += "Transport completion does not assert that requested writes or business outcomes succeeded. The root answer is retained in a runner-local response file.\n"
        else:
            report += "The maintenance result passed the caller's structured identity and status checks.\n"
            if wiki is not None:
                report += "\n| Field | Verified result |\n| --- | --- |\n"
                for field in ("source_repository", "pull_number", "merge_commit_sha", "wiki_repository", "wiki_sha"):
                    value = html.escape(safe_text(str(wiki.get(field, "")), self.secrets), quote=False).replace("|", "&#124;").replace("\n", " ")
                    report += f"| {field} | {value} |\n"
        if self.display_truncated:
            report += "\nLive display limits were reached; transport validation continued independently.\n"
        if self.io_failed:
            report += "\nLive display became unavailable; transport validation continued independently.\n"
        if mode == "full" and status != "failed":
            answer = text if status == "completed" else (wiki or {}).get("reason")
            if isinstance(answer, str) and answer:
                available = self.MAX_SUMMARY - len(report.encode("utf-8")) - 512
                rendered = html.escape(safe_text(answer, self.secrets), quote=False).replace("!", "&#33;")
                encoded = rendered.encode("utf-8")
                rendered = encoded[:available].decode("utf-8", errors="ignore")
                report += "\n### Agent response\n\n_This is agent-generated content, not a status assertion by the action._\n\n" + rendered + "\n"
                if len(encoded) > available:
                    report += "\n[Summary truncated; the complete final response remains in the runner-local file when available.]\n"
        return report
