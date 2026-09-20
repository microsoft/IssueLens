"""Extract only allowlisted identity fields from bounded tool results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _failed(value: Any) -> bool:
    if isinstance(value, Mapping):
        return value.get("isError") is True or value.get("is_error") is True
    return getattr(value, "is_error", False) is True


def result_metadata(result: Any) -> dict[str, Any]:
    failed = _failed(result)
    structured = (
        result.get("structured_content", result.get("structuredContent"))
        if isinstance(result, Mapping) else getattr(result, "structured_content", None)
    )
    if structured is None:
        content = result.get("content") if isinstance(result, Mapping) else getattr(result, "content", None)
        if not isinstance(content, str) or len(content) > 128 * 1024:
            return {"is_error": True} if failed else {}
        try:
            structured = json.loads(content)
        except (ValueError, RecursionError):
            return {"is_error": True} if failed else {}
    if not isinstance(structured, Mapping):
        return {"is_error": True} if failed else {}
    failed = failed or _failed(structured)
    nested = structured.get("structuredContent", structured.get("structured_content"))
    if isinstance(nested, Mapping):
        structured = nested
    metadata = {name: structured[name] for name in (
        "number", "id", "full_name", "source_repository", "wiki_repository", "status",
    ) if name in structured and type(structured[name]) in {str, int}}
    metadata["is_pull_request"] = "pull_request" in structured
    metadata["is_error"] = failed or _failed(structured)
    return metadata
