"""Copilot SDK tool registration for the github-access skill."""

from __future__ import annotations

import json
from typing import Any

from copilot.tools import Tool, ToolInvocation, ToolResult


TOOL_NAME = "github-access"
_MAX_RESULT_CHARS = 100_000


def create_tool(
    github_app: Any,
    *,
    provider: Any | None = None,
    client: Any | None = None,
) -> Tool | None:
    """Create the skill's trusted GitHub gateway when App access is configured."""
    if client is None:
        if provider is None:
            try:
                provider = github_app.GitHubAppTokenProvider.from_environment()
            except github_app.GitHubAppError:
                return None
        client = github_app.GitHubAppClient(provider)

    async def _github_access(invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments or {}
        try:
            result = await client.execute(
                arguments.get("operation", ""),
                arguments.get("repository", ""),
                issue_number=arguments.get("issue_number"),
                query=arguments.get("query"),
                path=arguments.get("path"),
                state=arguments.get("state", "open"),
                since=arguments.get("since"),
                labels=arguments.get("labels"),
                assignees=arguments.get("assignees"),
                per_page=arguments.get("per_page", 30),
            )
            text = json.dumps(result, ensure_ascii=True)
            if len(text) > _MAX_RESULT_CHARS:
                raise github_app.GitHubAppError(
                    "GitHub response is too large; narrow the query"
                )
            return ToolResult(text_result_for_llm=text)
        except github_app.GitHubAppError as exc:
            return ToolResult(
                text_result_for_llm=f"GitHub operation failed: {exc}",
                result_type="failure",
                error=str(exc),
            )

    return Tool(
        name=TOOL_NAME,
        description=(
            "The trusted GitHub gateway for the github-access skill. Access a "
            "repository through its IssueLens GitHub App installation. "
            "Credentials are resolved internally and never returned."
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "get-repository",
                        "list-issues",
                        "get-issue",
                        "list-comments",
                        "list-reactions",
                        "search-issues",
                        "list-labels",
                        "get-file",
                        "add-labels",
                        "set-assignees",
                    ],
                },
                "repository": {
                    "type": "string",
                    "description": "Target repository in owner/repository form.",
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "query": {"type": "string"},
                "path": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "default": "open",
                },
                "since": {
                    "type": "string",
                    "description": "Optional ISO 8601 updated-since timestamp.",
                },
                "labels": {"type": "array", "items": {"type": "string"}},
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "per_page": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 30,
                },
            },
            "required": ["operation", "repository"],
        },
        handler=_github_access,
    )
