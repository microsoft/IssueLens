"""Extract only allowlisted identity fields from bounded tool results."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from github_app_mcp.src.issuelens_github_mcp.outcomes import ERROR_TYPES


MAX_ERROR_MESSAGE = 1024
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+\S+")
_NAMED_SECRET = re.compile(
    r"""(?ix)
    ((?:"|')?
    (?:authorization|token|secret|password|sig|api[_-]?key|access[_-]?token|client[_-]?secret)
    (?:"|')?\s*[:=]\s*)
    (?:"[^"]*"|'[^']*'|[^\s,;]+)
    """
)
_GITHUB_SECRET = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
_URL_SECRET = re.compile(r"(?i)(https?://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?")


def _failed(value: Any) -> bool:
    if isinstance(value, Mapping):
        return (
            value.get("isError") is True or value.get("is_error") is True
            or value.get("success") is False or value.get("result_type") == "failure"
        )
    return getattr(value, "is_error", False) is True or getattr(value, "result_type", None) == "failure"


def safe_error_message(value: Any) -> str | None:
    """Bound tool-authored diagnostics while removing common credential carriers."""
    if not isinstance(value, str):
        return None
    message = "".join(
        " " if unicodedata.category(character)[0] == "C" else character
        for character in value
    )
    message = " ".join(message.split())
    message = _BEARER_SECRET.sub("Bearer <redacted>", message)
    message = _NAMED_SECRET.sub(r"\1<redacted>", message)
    message = _GITHUB_SECRET.sub("<redacted>", message)
    message = _URL_SECRET.sub(r"\1", message)
    if not message:
        return None
    if len(message) > MAX_ERROR_MESSAGE:
        message = message[:MAX_ERROR_MESSAGE - 3] + "..."
    return message


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
            metadata: dict[str, Any] = {"is_error": True}
            message = safe_error_message(content)
            if message is not None:
                metadata["error_message"] = message
            return metadata
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
            message = safe_error_message(error.get("message"))
            if message is not None:
                execution["error_message"] = message
            status = error.get("http_status")
            if type(status) is int and 100 <= status <= 599:
                execution["http_status"] = status
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
