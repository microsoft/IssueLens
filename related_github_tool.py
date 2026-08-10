"""Trusted anonymous reads for related public GitHub repositories."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from copilot.tools import Tool, ToolInvocation, ToolResult


TOOL_NAME = "issuelens-related-read"
_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_MAX_RESULT_CHARS = 100_000
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$"
)
_FORBIDDEN_QUERY_SCOPE_PATTERN = re.compile(
    r"(?i)(?:^|\s)(?:repo|org|user):"
)


class RelatedGitHubError(RuntimeError):
    """Raised when a public GitHub read cannot be completed."""


class PublicGitHubClient:
    """Perform bounded unauthenticated reads against GitHub public APIs."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def execute(
        self,
        operation: str,
        repository: str,
        *,
        issue_number: int | None = None,
        query: str | None = None,
        per_page: int = 30,
    ) -> Any:
        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise RelatedGitHubError("repository must use owner/repository format")
        per_page = max(1, min(per_page, 50))
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "IssueLens-related-public-read",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        params: dict[str, Any] = {}
        if operation == "search-issues":
            if not isinstance(query, str) or not query.strip():
                raise RelatedGitHubError("search-issues requires a query")
            if len(query) > 512:
                raise RelatedGitHubError("search-issues query exceeds 512 characters")
            if _FORBIDDEN_QUERY_SCOPE_PATTERN.search(query):
                raise RelatedGitHubError(
                    "search-issues query cannot set repository or owner scope"
                )
            url = f"{_API_ROOT}/search/issues"
            params = {
                "q": f"repo:{repository} is:issue {query.strip()}",
                "per_page": per_page,
            }
        elif operation in {"get-issue", "list-comments"}:
            if not isinstance(issue_number, int) or issue_number < 1:
                raise RelatedGitHubError(f"{operation} requires issue_number")
            suffix = "/comments" if operation == "list-comments" else ""
            url = (
                f"{_API_ROOT}/repos/{repository}/issues/{issue_number}{suffix}"
            )
            if suffix:
                params = {"per_page": per_page}
        else:
            raise RelatedGitHubError(f"Unsupported related read: {operation}")

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=30,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if (
                status == 403
                and error.response.headers.get("x-ratelimit-remaining") == "0"
            ):
                raise RelatedGitHubError(
                    "GitHub anonymous public-read rate limit exceeded"
                ) from error
            raise RelatedGitHubError(
                f"GitHub public read returned HTTP {status}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise RelatedGitHubError("GitHub public read failed") from error

        if operation == "search-issues" and isinstance(payload, dict):
            return payload.get("items", [])
        return payload


def create_tool(public_client: Any | None = None) -> Tool:
    """Create a fixed-operation, anonymous, public-read-only tool."""
    public_client = public_client or PublicGitHubClient()

    async def _related_read(invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments or {}
        repository = arguments.get("repository", "")
        try:
            result = await public_client.execute(
                arguments.get("operation", ""),
                repository,
                issue_number=arguments.get("issue_number"),
                query=arguments.get("query"),
                per_page=arguments.get("per_page", 30),
            )
            text = json.dumps(result, ensure_ascii=True)
            if len(text) > _MAX_RESULT_CHARS:
                raise RelatedGitHubError(
                    "GitHub public-read response is too large; narrow the query"
                )
            return ToolResult(text_result_for_llm=text)
        except RelatedGitHubError as error:
            return ToolResult(
                text_result_for_llm=f"Related GitHub read failed: {error}",
                result_type="failure",
                error=str(error),
            )

    return Tool(
        name=TOOL_NAME,
        description=(
            "Read issues and comments anonymously from a public GitHub "
            "repository for duplicate and related-issue evidence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repository": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": ["search-issues", "get-issue", "list-comments"],
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "query": {"type": "string"},
                "per_page": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 30,
                },
            },
            "required": ["repository", "operation"],
            "additionalProperties": False,
        },
        handler=_related_read,
    )
