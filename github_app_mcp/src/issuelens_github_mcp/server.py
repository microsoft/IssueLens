"""MCP tool registration and stdio entry point."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from mcp.server import MCPServer

from .auth import GitHubAppError, GitHubAppTokenProvider
from .config import ConfigurationError, GitHubAppConfig
from .github import GitHubClient


_ENABLE_WRITES_ENV = "GITHUB_MCP_ENABLE_WRITES"


def create_server(
    github: GitHubClient,
) -> MCPServer:
    """Create the IssueLens GitHub MCP server around a bounded client."""
    server = MCPServer(
        name="issuelens-github",
        title="IssueLens GitHub",
        description=(
            "Repository-confined GitHub issue triage tools backed by a "
            "GitHub App installation."
        ),
        instructions=(
            "Every tool requires an explicit owner/repository value. The "
            "server prefers repository-scoped GitHub App access and may fall "
            "back to anonymous access for bounded reads of public repositories."
        ),
        version="0.1.0",
    )

    @server.tool()
    async def get_repository(repository: str) -> Any:
        """Read metadata for an allowed owner/repository."""
        return await github.get_repository(repository)

    @server.tool()
    async def list_issues(
        repository: str,
        state: Literal["open", "closed", "all"] = "open",
        since: str | None = None,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List issues by update time in an allowed owner/repository."""
        return await github.list_issues(
            repository,
            state=state,
            since=since,
            per_page=per_page,
            page=page,
        )

    @server.tool()
    async def get_issue(repository: str, issue_number: int) -> Any:
        """Read one issue from an allowed owner/repository."""
        return await github.get_issue(repository, issue_number)

    @server.tool()
    async def list_issue_comments(
        repository: str,
        issue_number: int,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List comments on an issue in an allowed owner/repository."""
        return await github.list_issue_comments(
            repository,
            issue_number,
            per_page=per_page,
            page=page,
        )

    @server.tool()
    async def list_issue_reactions(
        repository: str,
        issue_number: int,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List issue reactions used for IssueLens criticality scoring."""
        return await github.list_issue_reactions(
            repository,
            issue_number,
            per_page=per_page,
            page=page,
        )

    @server.tool()
    async def search_issues(
        repository: str,
        query: str,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """Search issues without allowing query-controlled repository scope."""
        return await github.search_issues(
            repository,
            query,
            per_page=per_page,
            page=page,
        )

    @server.tool()
    async def list_labels(
        repository: str,
        per_page: int = 30,
        page: int = 1,
    ) -> Any:
        """List existing labels in an allowed owner/repository."""
        return await github.list_labels(
            repository,
            per_page=per_page,
            page=page,
        )

    @server.tool()
    async def get_file(repository: str, path: str) -> Any:
        """Read one bounded UTF-8 file or directory listing from a repository."""
        return await github.get_file(repository, path)

    if github.writes_enabled:

        @server.tool()
        async def add_labels(
            repository: str,
            issue_number: int,
            labels: list[str],
        ) -> Any:
            """Add existing labels to an issue without removing current labels."""
            return await github.add_labels(repository, issue_number, labels)

        @server.tool()
        async def set_assignees(
            repository: str,
            issue_number: int,
            assignees: list[str],
        ) -> Any:
            """Replace an issue's complete assignee list."""
            return await github.set_assignees(
                repository,
                issue_number,
                assignees,
            )

        @server.tool()
        async def add_issue_comment(
            repository: str,
            issue_number: int,
            body: str,
        ) -> Any:
            """Post one issue-triage comment to an issue."""
            return await github.add_issue_comment(
                repository,
                issue_number,
                body,
            )

    return server


def build_server_from_environment(
    environment: Mapping[str, str] | None = None,
) -> MCPServer:
    """Build a server using only validated environment configuration."""
    environment = os.environ if environment is None else environment
    app_config = GitHubAppConfig.from_environment(environment)
    writes_enabled = _boolean(
        environment.get(_ENABLE_WRITES_ENV, "false"),
        _ENABLE_WRITES_ENV,
    )
    provider = GitHubAppTokenProvider(app_config)
    github = GitHubClient(
        provider,
        writes_enabled=writes_enabled,
    )
    return create_server(github)


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def main() -> None:
    """Run the server over stdio without writing non-protocol data to stdout."""
    try:
        server = build_server_from_environment()
    except (ConfigurationError, GitHubAppError) as error:
        raise SystemExit(f"IssueLens GitHub MCP configuration failed: {error}") from error
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
