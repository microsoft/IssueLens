"""Copilot SDK tool for validated IssueLens repository instructions."""

from __future__ import annotations

from typing import Any

from copilot.tools import Tool, ToolInvocation

from issuelens_config import (
    INSTRUCTION_DOMAINS,
    load_instruction,
)
from tool_results import tool_handler


TOOL_NAME = "issuelens-config"


def create_tool(client: Any) -> Tool:
    """Create a repository-config tool backed by one protocol's GitHub client."""

    async def _get_instruction(invocation: ToolInvocation) -> Any:
        arguments = invocation.arguments or {}
        return await load_instruction(
            client,
            arguments.get("repository", ""),
            arguments.get("domain", ""),
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
        handler=tool_handler(_get_instruction),
    )
