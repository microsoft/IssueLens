"""Extract only allowlisted identity fields from bounded tool results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from github_app_mcp.src.issuelens_github_mcp.outcomes import (
    ERROR_TYPES,
    telemetry_error_message,
)


def _failed(value: Any) -> bool:
    if isinstance(value, Mapping):
        return (
            value.get("isError") is True or value.get("is_error") is True
            or value.get("success") is False or value.get("result_type") == "failure"
        )
    return getattr(value, "is_error", False) is True or getattr(value, "result_type", None) == "failure"


def _content_text(result: Any) -> str | None:
    if isinstance(result, str):
        return result
    content = (
        result.get("content")
        if isinstance(result, Mapping) else getattr(result, "content", None)
    )
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)) and len(content) == 1:
        item = content[0]
        text = item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
        if isinstance(text, str):
            return text
    return None


def result_metadata(result: Any) -> dict[str, Any]:
    failed = _failed(result)
    structured = (
        result.get("structured_content", result.get("structuredContent"))
        if isinstance(result, Mapping) else getattr(result, "structured_content", None)
    )
    if structured is None:
        content = _content_text(result)
        if not isinstance(content, str) or len(content) > 128 * 1024:
            return {"is_error": True} if failed else {}
        try:
            structured = json.loads(content)
        except (ValueError, RecursionError):
            if not failed:
                return {}
            return {"is_error": True}
    if not isinstance(structured, Mapping):
        return {"is_error": True} if failed else {}
    failed = failed or _failed(structured)
    nested = structured.get("structuredContent", structured.get("structured_content"))
    if isinstance(nested, Mapping):
        structured = nested
    execution: dict[str, Any] = {}
    outcome = structured.get("outcome")
    if type(structured.get("success")) is bool and isinstance(outcome, str) and outcome in {
        "completed", "not_applied", "unknown",
    }:
        failed = failed or structured["success"] is False or structured.get("error") is not None
        execution["tool_outcome"] = outcome
        error = structured.get("error")
        if isinstance(error, Mapping):
            error_type = error.get("type")
            if isinstance(error_type, str) and error_type in ERROR_TYPES:
                execution["error_type"] = error_type
            status = error.get("http_status")
            if type(status) is int and 100 <= status <= 599:
                execution["http_status"] = status
            else:
                status = None
            if isinstance(error_type, str) and error_type in ERROR_TYPES:
                expected_message = telemetry_error_message(error_type, status)
                if error.get("telemetry_message") == expected_message:
                    execution["error_message"] = expected_message
        structured = structured.get("result")
        if not isinstance(structured, Mapping):
            return {**execution, "is_error": failed}
    metadata = {name: structured[name] for name in (
        "number", "id", "full_name", "source_repository", "wiki_repository", "status",
    ) if name in structured and type(structured[name]) in {str, int}}
    metadata["is_pull_request"] = "pull_request" in structured
    metadata.update(execution)
    metadata["is_error"] = failed or _failed(structured)
    return metadata
