"""Copilot SDK tool for validated IssueLens repository instructions."""

from __future__ import annotations

import json
from typing import Any

from copilot.tools import Tool, ToolInvocation, ToolResult

from issuelens_config import (
    INSTRUCTION_DOMAINS,
    IssueLensConfigError,
    load_instruction,
)


TOOL_NAME = "issuelens-config"


def create_tool(client: Any) -> Tool:
    """Create a repository-config tool backed by one protocol's GitHub client."""

    async def _get_instruction(invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments or {}
        try:
            result = await load_instruction(
                client,
                arguments.get("repository", ""),
                arguments.get("domain", ""),
            )
            return ToolResult(
                text_result_for_llm=json.dumps(result, ensure_ascii=True)
            )
        except IssueLensConfigError as error:
            return ToolResult(
                text_result_for_llm=f"IssueLens configuration failed: {error}",
                result_type="failure",
                error=str(error),
            )

    return Tool(
        name=TOOL_NAME,
        description=(
            "Load validated, capability-scoped IssueLens instructions from the "
            "target repository. Falls back to legacy instruction files or "
            "built-in behavior when .github/issuelens.yml is absent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": {
                    "type": "string",
                    "description": "Target repository in owner/repository form.",
                },
                "domain": {
                    "type": "string",
                    "enum": sorted(INSTRUCTION_DOMAINS),
                },
            },
            "required": ["repository", "domain"],
            "additionalProperties": False,
        },
        handler=_get_instruction,
    )
