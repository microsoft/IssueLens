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
WRITE_TOOLS = frozenset({
    "add_labels", "set_assignees", "add_issue_comment", "add_eyes_reaction",
    "write_wiki_pages", "send-email", "send-teams-notification",
})


class ErrorDetails(TypedDict):
    type: str
    message: str
    http_status: int | None


class ToolOutcome(TypedDict):
    success: bool
    outcome: Outcome
    result: Any
    error: ErrorDetails | None


class ToolFailure(RuntimeError):
    """An explicitly safe message, classification, and known execution outcome."""

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


def failure_result(error: ToolFailure) -> ToolOutcome:
    return {
        "success": False,
        "outcome": error.outcome,
        "result": None,
        "error": {
            "type": error.error_type if error.error_type in ERROR_TYPES else "tool_error",
            "message": str(error),
            "http_status": error.http_status,
        },
    }


def http_error_type(status: int) -> str:
    return {
        400: "invalid_input", 401: "authentication_error", 403: "permission_denied",
        404: "not_found", 408: "timeout", 409: "conflict", 412: "conflict",
        422: "invalid_input", 429: "rate_limited", 504: "timeout",
    }.get(status, "upstream_error")
