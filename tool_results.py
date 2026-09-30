"""Adapt the common execution contract to Copilot's native tool status."""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable

from copilot.tools import ToolInvocation, ToolResult

from github_app_mcp.src.issuelens_github_mcp.outcomes import (
    ToolFailure, ToolOutcome, failure_result, success_result,
)


def copilot_result(outcome: ToolOutcome) -> ToolResult:
    error = outcome["error"]
    encoded = json.dumps(outcome, ensure_ascii=True, allow_nan=False)
    return ToolResult(
        text_result_for_llm=encoded,
        result_type="success" if outcome["success"] else "failure",
        error=encoded if error else None,
    )


def tool_handler(
    operation: Callable[[ToolInvocation], Awaitable[object]], *, write: bool = False,
) -> Callable[[ToolInvocation], Awaitable[ToolResult]]:
    @functools.wraps(operation)
    async def execute(invocation: ToolInvocation) -> ToolResult:
        try:
            if invocation.arguments is not None and not isinstance(invocation.arguments, dict):
                raise ToolFailure("Tool arguments must be an object.", error_type="invalid_input")
            return copilot_result(success_result(await operation(invocation)))
        except ToolFailure as error:
            return copilot_result(failure_result(error))
        except Exception:
            return copilot_result(failure_result(ToolFailure(
                "Tool execution failed unexpectedly; inspect the current state before another write.",
                error_type="internal_error", outcome="unknown" if write else "not_applied",
            )))
    return execute
