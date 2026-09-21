"""MCP tool registration and stdio entry point."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field

from .auth import GitHubAppError, GitHubAppTokenProvider
from .config import ConfigurationError, GitHubAppConfig
from .github import CommitDetail, GitHubClient, ReactionTarget


_ENABLE_WRITES_ENV = "GITHUB_MCP_ENABLE_WRITES"
_PerPage = Annotated[int, Field(strict=True, ge=1, le=100)]
_FilePage = Annotated[int, Field(strict=True, ge=1, le=3000)]


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
            "back to anonymous access for bounded reads of public repositories. "
            "For wiki tools, repository is always the source project. Its "
            "validated team-memory customization selects the wiki repository, "
            "which requires App access; wiki operations never use anonymous access."
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
    async def get_issue_comment(
        repository: str,
        issue_number: int,
        comment_id: int,
    ) -> Any:
        """Read one comment only when it belongs to the target issue."""
        return await github.get_issue_comment(
            repository,
            issue_number,
            comment_id,
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
    async def get_file(repository: str, path: str, ref: str | None = None) -> Any:
        """Read one UTF-8 file (up to 64 KiB) or directory; use a full commit ref for pinned context."""
        return await github.get_file(repository, path, ref=ref)

    @server.tool()
    async def get_pull_request(repository: str, pull_number: int) -> Any:
        """Read bounded PR metadata, including base/head SHAs and changed_files."""
        return await github.get_pull_request(repository, pull_number)

    @server.tool()
    async def list_pull_request_files(
        repository: str, pull_number: int, per_page: _PerPage = 30, page: _FilePage = 1,
    ) -> Any:
        """Read one page of changed PR files, including available patches.

        For large responses, reduce per_page, down to 1, and restart pagination
        at page 1 when changing its size. Empty or short pages end enumeration.
        Missing patches do not mean unchanged files; use pinned get_file reads
        where supported or report missing evidence. Verify PR SHAs before and
        after paging because this endpoint cannot pin a commit.
        This REST endpoint has a 3,000-file ceiling; paging does not remove it.
        """
        return await github.list_pull_request_files(repository, pull_number, per_page=per_page, page=page)

    @server.tool()
    async def list_pull_request_commits(repository: str, pull_number: int, per_page: int = 30, page: int = 1) -> Any:
        """List bounded PR commit metadata (GitHub's 250-commit endpoint ceiling applies)."""
        return await github.list_pull_request_commits(repository, pull_number, per_page=per_page, page=page)

    @server.tool()
    async def list_pull_request_reviews(repository: str, pull_number: int, per_page: int = 30, page: int = 1) -> Any:
        """List one bounded page of reviews on an explicit repository's PR."""
        return await github.list_pull_request_reviews(repository, pull_number, per_page=per_page, page=page)

    @server.tool()
    async def list_pull_request_review_comments(repository: str, pull_number: int, per_page: int = 30, page: int = 1) -> Any:
        """List one bounded page of individual PR review comments, not review threads."""
        return await github.list_pull_request_review_comments(repository, pull_number, per_page=per_page, page=page)

    @server.tool()
    async def get_commit(
        repository: str, sha: str, detail: CommitDetail = "stats",
        per_page: _PerPage = 30, page: _FilePage = 1,
    ) -> Any:
        """Read a commit with upstream-style detail and file pagination controls.

        detail=stats (default) returns file metadata without patches; none omits
        files and aggregate stats; full_patch includes available patches.
        Commit identity, parents, and tree metadata are retained in every mode.
        Use a full SHA for stable paging. per_page/page apply to files, not
        commits; at most 3,000 files are available. Reduce per_page after a size
        error. A missing patch is not proof of no change. HTTP download is
        bounded to 1 MiB; the final projected JSON remains at most 100,000 bytes.
        """
        return await github.get_commit(
            repository, sha, detail=detail, per_page=per_page, page=page,
        )

    @server.tool()
    async def compare_commits(repository: str, base: str, head: str) -> Any:
        """Read a bounded REST comparison, including at most 300 file patches.

        For PR-wide evidence, prefer list_pull_request_files with small pages.
        This tool cannot paginate or guarantee exhaustive evidence for large changes.
        """
        return await github.compare_commits(repository, base, head)

    @server.tool()
    async def list_repository_tree(repository: str, ref: str, recursive: bool = True) -> Any:
        """Read one bounded Git tree; honor truncated and use smaller subtree reads when needed."""
        return await github.list_repository_tree(repository, ref, recursive=recursive)

    @server.tool()
    async def search_repository_content(repository: str, query: str, ref: str | None = None, per_page: int = 30, page: int = 1) -> Any:
        """Search content in one explicit repository; query cannot contain qualifiers.

        With ref, resolve a branch, tag, or commit to one immutable commit and
        search its regular UTF-8 files for the trimmed, case-insensitive literal
        query within each line, not in paths. No regex or search operators apply.
        At most 64 regular files, 256 KiB eligible content, and 66 content API
        requests are allowed for a full SHA, plus initial authentication
        lookup/minting overhead. Branch/tag resolution uses fixed Git-ref/tag
        routes (up to ten additional requests), never patch-bearing commits.
        One HTTP client is reused for the scan's content requests and closed on
        success, error, cancellation, or deadline expiry. A fixed 60-second overall
        scan time budget covers authentication, response-body reads, and local
        result construction without resetting per request. Deadline expiry fails
        explicitly without partial results or indexed fallback; host cancellation
        propagates after cleanup. Files over 64 KiB, binary/non-UTF-8 content, unsupported
        encodings, symlinks, and submodules are skipped explicitly. Truncated
        trees, exhausted scan limits, and malformed responses fail without an
        indexed fallback. HTTP responses are capped at 128 KiB and returned
        results at 100,000 bytes.
        Items are sorted by path then paginated; total_count counts matching
        files in the scanned subset. Check incomplete_results and skipped_reasons
        before treating zero matches as exhaustive. Each item has a blob SHA,
        commit-pinned URL, and at most three matching line numbers with excerpts
        capped at the first 160 characters. Reuse resolved_ref for later pages
        to avoid a moving branch changing the snapshot; reduce per_page if the
        result exceeds the response limit.

        Without ref, use GitHub's indexed default-branch code search and return
        its native result/pagination semantics, not immutable source evidence.
        """
        return await github.search_repository_content(repository, query, ref=ref, per_page=per_page, page=page)

    @server.tool()
    async def list_merged_pull_requests(repository: str, base: str, since: str | None = None, per_page: int = 30, page: int = 1) -> Any:
        """Search one repository's merged PRs for an explicit base branch and optional since time."""
        return await github.list_merged_pull_requests(repository, base=base, since=since, per_page=per_page, page=page)

    @server.tool()
    async def get_wiki_snapshot(repository: str) -> Any:
        """Read the source project's configured wiki snapshot using App access."""
        return await github.get_wiki_snapshot(repository)

    @server.tool()
    async def list_wiki_pages(repository: str, ref: str = "HEAD") -> Any:
        """List pages in the source project's configured wiki using App access.

        Returns source_repository, wiki_repository, and the page list in result.
        """
        return await github.list_wiki_pages(repository, ref)

    @server.tool()
    async def get_wiki_page(repository: str, path: str, ref: str = "HEAD") -> Any:
        """Read a page in the source project's configured wiki using App access."""
        return await github.get_wiki_page(repository, path, ref)

    @server.tool()
    async def search_wiki(repository: str, query: str, ref: str = "HEAD") -> Any:
        """Search the source project's configured wiki using App access.

        Returns source_repository, wiki_repository, and the matching page list in result.
        """
        return await github.search_wiki(repository, query, ref)

    @server.tool()
    async def list_wiki_history(
        repository: str, path: str | None = None, limit: int = 30, ref: str = "HEAD"
    ) -> Any:
        """Read history in the source project's configured wiki using App access.

        Returns source_repository, wiki_repository, and the commit SHA list in result.
        """
        return await github.list_wiki_history(repository, path, limit, ref)

    @server.tool()
    async def get_wiki_diff(repository: str, base: str, head: str = "HEAD") -> Any:
        """Diff snapshots in the source project's configured wiki using App access.

        Returns source_repository, wiki_repository, and the diff text in result.
        """
        return await github.get_wiki_diff(repository, base, head)

    if github.wiki_writes_enabled:

        @server.tool()
        async def write_wiki_pages(
            repository: str,
            pages: dict[str, str],
            expected_base: str,
            message: str,
            expected_wiki_repository: str,
        ) -> Any:
            """Write source-project memory to its configured wiki using App access.

            repository is the source project, never a raw wiki destination or
            remote. Validated team-memory customization resolves the destination;
            its App installation and contents-write token authorize access.
            Read a new wiki snapshot first and pass its wiki_repository as
            expected_wiki_repository and its full SHA as expected_base. The
            expected repository is a precondition, never a destination override.
            If the destination changes, read a fresh snapshot before writing.
            Paths must be relative Markdown pages: 1-20 pages, at most 64 KiB per
            page and 256 KiB per batch. The single-line message is at most 512
            bytes. The backend validates all paths, refs, and limits and rejects
            conflicting snapshots. Commits use the verified App Bot identity.
            """
            return await github.write_wiki_pages(
                repository, pages, expected_base, message,
                expected_wiki_repository=expected_wiki_repository,
            )

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

        @server.tool()
        async def add_eyes_reaction(
            repository: str,
            target_kind: ReactionTarget,
            target_id: int,
        ) -> Any:
            """Add the fixed eyes reaction to one supported activity."""
            return await github.add_eyes_reaction(
                repository,
                target_kind,
                target_id,
            )

    return server


def build_server_from_environment(
    environment: Mapping[str, str] | None = None,
    *,
    wiki_writer: bool = False,
) -> MCPServer:
    """Build a server with validated credentials and an explicit internal role."""
    if not isinstance(wiki_writer, bool):
        raise ConfigurationError("wiki_writer must be a boolean")
    environment = os.environ if environment is None else environment
    app_config = GitHubAppConfig.from_environment(environment)
    writes_enabled = False if wiki_writer else _boolean(
        environment.get(_ENABLE_WRITES_ENV, "false"),
        _ENABLE_WRITES_ENV,
    )
    provider = GitHubAppTokenProvider(app_config)
    github = GitHubClient(
        provider,
        writes_enabled=writes_enabled,
        wiki_writes_enabled=wiki_writer,
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
    parser = argparse.ArgumentParser(description="IssueLens GitHub MCP server")
    parser.add_argument(
        "--wiki-writer", action="store_true",
        help="Internal wiki-writer role; disables issue writes",
    )
    options = parser.parse_args()
    try:
        server = build_server_from_environment(wiki_writer=options.wiki_writer)
    except (ConfigurationError, GitHubAppError) as error:
        raise SystemExit(f"IssueLens GitHub MCP configuration failed: {error}") from error
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
