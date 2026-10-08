"""Content-safe execution outcomes shared by MCP and host tools."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


Outcome = Literal["completed", "not_applied", "unknown"]
ERROR_TYPES = frozenset({
    "tool_error", "github_error", "wiki_error", "invalid_input",
    "configuration_error", "authentication_error", "permission_denied",
    "not_found", "conflict", "destination_changed", "publish_rejected",
    "rate_limited", "timeout", "transport_error", "limit_exceeded",
    "upstream_error", "invalid_response", "unsafe_result", "internal_error",
    "outcome_unknown",
})
_TELEMETRY_MESSAGES = {
    "tool_error": "Tool operation failed.",
    "github_error": "GitHub operation failed.",
    "wiki_error": "Wiki operation failed.",
    "invalid_input": "Tool input was rejected.",
    "configuration_error": "Tool configuration is invalid.",
    "authentication_error": "Tool authentication failed.",
    "permission_denied": "Tool permission was denied.",
    "not_found": "Requested resource was not found.",
    "conflict": "Tool operation conflicted with current state.",
    "destination_changed": "Configured destination changed during the operation.",
    "publish_rejected": "Publication was rejected.",
    "rate_limited": "Upstream service rate limit was reached.",
    "timeout": "Tool operation timed out.",
    "transport_error": "Tool transport failed.",
    "limit_exceeded": "Tool operation exceeded a configured limit.",
    "upstream_error": "Upstream service request failed.",
    "invalid_response": "Upstream service returned an invalid response.",
    "unsafe_result": "Tool result was rejected as unsafe.",
    "internal_error": "Tool execution failed unexpectedly.",
    "outcome_unknown": "Tool outcome could not be confirmed.",
}
WRITE_TOOLS = frozenset({
    "add_labels", "set_assignees", "add_issue_comment", "add_eyes_reaction",
    "write_wiki_pages", "send-email", "send-teams-notification",
})


class ErrorDetails(TypedDict):
    type: str
    message: str
    telemetry_message: str
    http_status: int | None


class ToolOutcome(TypedDict):
    success: bool
    outcome: Outcome
    result: Any
    error: ErrorDetails | None


class ToolFailure(RuntimeError):
    """A bounded agent-facing message, classification, and known execution outcome."""

    default_type = "tool_error"

    def __init__(
        self, message: str, *, error_type: str | None = None,
        outcome: Outcome = "not_applied", http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type or self.default_type
        self.outcome = outcome
        self.http_status = http_status


def success_result(result: Any) -> ToolOutcome:
    return {"success": True, "outcome": "completed", "result": result, "error": None}


def telemetry_error_message(error_type: str, http_status: int | None) -> str:
    error_type = error_type if error_type in ERROR_TYPES else "tool_error"
    telemetry_message = _TELEMETRY_MESSAGES[error_type]
    if type(http_status) is int and 100 <= http_status <= 599:
        telemetry_message = f"{telemetry_message[:-1]} (HTTP {http_status})."
    return telemetry_message


def failure_result(error: ToolFailure) -> ToolOutcome:
    error_type = error.error_type if error.error_type in ERROR_TYPES else "tool_error"
    return {
        "success": False,
        "outcome": error.outcome,
        "result": None,
        "error": {
            "type": error_type,
            "message": str(error),
            "telemetry_message": telemetry_error_message(error_type, error.http_status),
            "http_status": error.http_status,
        },
    }


def http_error_type(status: int) -> str:
    return {
        400: "invalid_input", 401: "authentication_error", 403: "permission_denied",
        404: "not_found", 408: "timeout", 409: "conflict", 412: "conflict",
        422: "invalid_input", 429: "rate_limited", 504: "timeout",
    }.get(status, "upstream_error")
