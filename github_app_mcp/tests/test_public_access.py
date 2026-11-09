"""Public-context confidentiality checks across every repository data path."""

import base64
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mcp import Client

sys.path.insert(0, os.fspath(Path(__file__).parents[1] / "src"))

from issuelens_github_mcp.auth import GitHubAppError, InstallationCredential
from issuelens_github_mcp.config import ConfigurationError
from issuelens_github_mcp.github import GitHubClient
from issuelens_github_mcp.policy import IssueLensConfigError, load_instruction
from issuelens_github_mcp.server import build_server_from_environment, create_server


REPOSITORY = "microsoft/IssueLens"
CANARY = "PRIVATE-CONTENT-CANARY"
COMMIT, TREE, BLOB = "a" * 40, "b" * 40, "c" * 40
PUBLIC = {"full_name": REPOSITORY, "visibility": "public", "private": False}


class TokenProvider:
    def __init__(self, *, anonymous=False):
        self.calls = []
        self.anonymous = anonymous

    async def get_token(self, repository, permissions):
        self.calls.append((repository, permissions))
        if self.anonymous:
            raise GitHubAppError("App installation unavailable")
        return InstallationCredential(
            installation_id=1, repository=repository,
            permissions=tuple(sorted(permissions.items())),
            token="test-token", expires_at=float("inf"),
        )


class PublicAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.provider = TokenProvider()

    def client(self, metadata, *, writes=False, wiki_writes=False, material=None, provider=None):
        def handler(request):
            self.requests.append(request)
            if request.url.path == f"/repos/{REPOSITORY}":
                value = metadata() if callable(metadata) else metadata
                return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
            return material(request) if material else httpx.Response(200, json={"body": CANARY})

        return GitHubClient(
            provider or self.provider, public_only=True, writes_enabled=writes,
            wiki_writes_enabled=wiki_writes, transport=httpx.MockTransport(handler),
        )

    async def test_nonpublic_or_unverified_repositories_deny_every_data_path_before_content_access(self):
        operations = {
            "metadata": lambda c: c.get_repository(REPOSITORY),
            "issues": lambda c: c.list_issues(REPOSITORY),
            "issue": lambda c: c.get_issue(REPOSITORY, 1),
            "comments": lambda c: c.list_issue_comments(REPOSITORY, 1),
            "comment": lambda c: c.get_issue_comment(REPOSITORY, 1, 1),
            "reactions": lambda c: c.list_issue_reactions(REPOSITORY, 1),
            "labels": lambda c: c.list_labels(REPOSITORY),
            "issue search": lambda c: c.search_issues(REPOSITORY, "crash"),
            "file": lambda c: c.get_file(REPOSITORY, "README.md"),
            "PR": lambda c: c.get_pull_request(REPOSITORY, 1),
            "PR manifest": lambda c: c.list_pull_request_file_manifest(REPOSITORY, 1),
            "PR patches": lambda c: c.list_pull_request_files(REPOSITORY, 1),
            "PR commits": lambda c: c.list_pull_request_commits(REPOSITORY, 1),
            "PR reviews": lambda c: c.list_pull_request_reviews(REPOSITORY, 1),
            "PR review comments": lambda c: c.list_pull_request_review_comments(REPOSITORY, 1),
            "commit": lambda c: c.get_commit(REPOSITORY, COMMIT),
            "compare": lambda c: c.compare_commits(REPOSITORY, COMMIT, TREE),
            "tree": lambda c: c.list_repository_tree(REPOSITORY, COMMIT),
            "indexed source": lambda c: c.search_repository_content(REPOSITORY, "crash"),
            "pinned source": lambda c: c.search_repository_content(REPOSITORY, "crash", ref=COMMIT),
            "merged PRs": lambda c: c.list_merged_pull_requests(REPOSITORY, base="main"),
            "wiki": lambda c: c.get_wiki_snapshot(REPOSITORY),
            "wiki pages": lambda c: c.list_wiki_pages(REPOSITORY),
            "wiki page": lambda c: c.get_wiki_page(REPOSITORY, "Home.md"),
            "wiki search": lambda c: c.search_wiki(REPOSITORY, "crash"),
            "wiki history": lambda c: c.list_wiki_history(REPOSITORY),
            "wiki diff": lambda c: c.get_wiki_diff(REPOSITORY, COMMIT, TREE),
            "images": lambda c: c.get_issue_images(REPOSITORY, 1),
            "issue write": lambda c: c.add_issue_comment(REPOSITORY, 1, "Report"),
            "label write": lambda c: c.add_labels(REPOSITORY, 1, ["bug"]),
            "assignment": lambda c: c.set_assignees(REPOSITORY, 1, ["octocat"]),
            "reaction write": lambda c: c.add_eyes_reaction(REPOSITORY, "issue", 1),
            "wiki write": lambda c: c.write_wiki_pages(
                REPOSITORY, {"Home.md": "Memory"}, COMMIT, "Update",
                expected_wiki_repository=REPOSITORY,
            ),
            "wiki patch": lambda c: c.patch_wiki_pages(
                REPOSITORY, {}, COMMIT, "Update",
                expected_wiki_repository=REPOSITORY,
            ),
        }
        for metadata in (
            {**PUBLIC, "visibility": "private", "private": True, "description": CANARY},
            {**PUBLIC, "visibility": "internal", "private": True, "description": CANARY},
            {"full_name": REPOSITORY, "description": CANARY},
            {**PUBLIC, "private": True},
            {**PUBLIC, "full_name": "microsoft/Other"},
            [CANARY],
        ):
            for name, operation in operations.items():
                with self.subTest(metadata=metadata, operation=name):
                    self.requests.clear()
                    self.provider.calls.clear()
                    client = self.client(metadata, writes=True, wiki_writes=True)
                    with self.assertRaises(GitHubAppError) as caught:
                        await operation(client)
                    self.assertEqual(caught.exception.error_type, "permission_denied")
                    self.assertEqual(caught.exception.outcome, "not_applied")
                    self.assertNotIn(CANARY, str(caught.exception))
                    self.assertEqual([r.url.path for r in self.requests], [f"/repos/{REPOSITORY}"])
                    self.assertEqual(self.provider.calls, [(REPOSITORY, {"metadata": "read"})])

    async def test_authenticated_public_reads_remain_available_across_eight_repositories(self):
        repositories = [REPOSITORY, *(f"microsoft/java-tooling-{index}" for index in range(7))]

        def handler(request):
            self.requests.append(request)
            parts = request.url.path.split("/")
            repository = "/".join(parts[2:4])
            if len(parts) == 4:
                return httpx.Response(200, json={**PUBLIC, "full_name": repository})
            return httpx.Response(200, json={"body": f"Public issue in {repository}"})

        client = GitHubClient(
            self.provider, public_only=True, transport=httpx.MockTransport(handler),
        )
        for repository in repositories:
            result = await client.get_issue(repository, 1)
            self.assertEqual(result["body"], f"Public issue in {repository}")
        self.assertEqual(len(self.requests), 3 * len(repositories))
        for request in self.requests:
            self.assertIn("Authorization", request.headers)
        self.assertEqual(self.provider.calls, [
            (repository, permissions)
            for repository in repositories
            for permissions in ({"metadata": "read"}, {"issues": "read"}, {"metadata": "read"})
        ])

    async def test_unknown_visibility_is_not_a_missing_policy_or_a_successful_builtin_fallback(self):
        for metadata in (
            {**PUBLIC, "visibility": "private", "private": True},
            httpx.Response(404, json={"message": CANARY}),
            httpx.Response(403, json={"message": CANARY}),
            httpx.Response(302, headers={"Location": "https://example.invalid"}),
        ):
            with self.subTest(metadata=metadata):
                self.requests.clear()
                with self.assertRaises(IssueLensConfigError) as caught:
                    await load_instruction(self.client(metadata), REPOSITORY, "duplicate_detection")
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertEqual(len(self.requests), 1)

    async def test_visibility_is_rechecked_before_read_results_or_write_acknowledgements_are_released(self):
        for write in (False, True):
            with self.subTest(write=write):
                self.requests.clear()
                checks = iter((PUBLIC, {**PUBLIC, "visibility": "private", "private": True}))
                client = self.client(lambda: next(checks), writes=write)
                with self.assertRaises(GitHubAppError) as caught:
                    if write:
                        await client.add_issue_comment(REPOSITORY, 1, "Public report")
                    else:
                        await client.get_issue(REPOSITORY, 1)
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertEqual(caught.exception.outcome, "unknown" if write else "not_applied")
                self.assertEqual(len(self.requests), 3)
                self.assertEqual(sum(r.method == "POST" for r in self.requests), int(write))

    async def test_public_reads_keep_app_rate_limits_and_anonymous_fallback(self):
        for anonymous in (False, True):
            with self.subTest(anonymous=anonymous):
                self.requests.clear()
                provider = TokenProvider(anonymous=anonymous)
                result = await self.client(PUBLIC, provider=provider).get_issue(REPOSITORY, 1)
                self.assertEqual(result, {"body": CANARY})
                self.assertEqual(len(self.requests), 3)
                self.assertTrue(all(
                    ("Authorization" in r.headers) == (not anonymous) for r in self.requests
                ))

    async def test_pinned_source_search_rechecks_visibility_with_its_separate_scan_credential(self):
        def material(request):
            if request.url.path.endswith(f"/git/commits/{COMMIT}"):
                return httpx.Response(200, json={"sha": COMMIT, "tree": {"sha": TREE}})
            if request.url.path.endswith(f"/git/trees/{TREE}"):
                return httpx.Response(200, json={
                    "sha": TREE, "truncated": False,
                    "tree": [{"path": "source.py", "sha": BLOB, "type": "blob", "mode": "100644", "size": 5}],
                })
            return httpx.Response(200, json={
                "sha": BLOB, "size": 5, "encoding": "base64",
                "content": base64.b64encode(b"crash").decode(),
            })

        result = await self.client(PUBLIC, material=material).search_repository_content(
            REPOSITORY, "crash", ref=COMMIT,
        )
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["resolved_ref"], COMMIT)
        self.assertEqual(self.requests[-1].url.path, f"/repos/{REPOSITORY}")
        self.assertEqual(sum(r.url.path == f"/repos/{REPOSITORY}" for r in self.requests), 8)

    async def test_public_issue_images_do_not_send_app_credentials_to_attachment_hosts(self):
        asset = "https://private-user-images.githubusercontent.com/1/image.png"

        def material(request):
            if request.url.host == "api.github.com":
                return httpx.Response(200, json={"body": f"![image]({asset})"})
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(403, text=CANARY)

        result = await self.client(PUBLIC, material=material).get_issue_images(REPOSITORY, 1)
        self.assertEqual(result["images"], [])
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertTrue(any(r.url.host == "private-user-images.githubusercontent.com" for r in self.requests))

    async def test_public_wiki_rejects_nonpublic_destination_before_git_access(self):
        client = self.client(PUBLIC)
        original = client.get_repository

        async def metadata(repository):
            if repository != REPOSITORY:
                return client._check_public_metadata(repository, {
                    "full_name": repository, "visibility": "private", "private": True, "description": CANARY,
                })
            return await original(repository)

        with (
            patch("issuelens_github_mcp.github.resolve_wiki_repository", AsyncMock(return_value="microsoft/Other")),
            patch.object(client, "get_repository", side_effect=metadata),
            patch.object(client, "_wiki_operation") as git,
        ):
            with self.assertRaises(GitHubAppError) as caught:
                await client.get_wiki_snapshot(REPOSITORY)
        git.assert_not_called()
        self.assertNotIn(CANARY, str(caught.exception))

    async def test_public_wiki_sanitizes_backend_errors_and_preserves_unknown_publication(self):
        for write in (False, True):
            with self.subTest(write=write):
                client = self.client(PUBLIC, wiki_writes=write)
                self.provider.get_bot_identity = AsyncMock(return_value=("App", "app@example.invalid"))
                with (
                    patch("issuelens_github_mcp.github.resolve_wiki_repository", AsyncMock(return_value=REPOSITORY)),
                    patch.object(client, "_wiki_operation", side_effect=GitHubAppError(
                        CANARY, outcome="unknown" if write else "not_applied",
                    )),
                ):
                    with self.assertRaises(GitHubAppError) as caught:
                        if write:
                            await client.write_wiki_pages(
                                REPOSITORY, {"Home.md": "Memory"}, COMMIT, "Update",
                                expected_wiki_repository=REPOSITORY,
                            )
                        else:
                            await client.get_wiki_snapshot(REPOSITORY)
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertEqual(caught.exception.outcome, "unknown" if write else "not_applied")

    async def test_private_reads_remain_available_in_a_readonly_internal_client(self):
        client = self.client({**PUBLIC, "visibility": "private", "private": True})
        client = GitHubClient(self.provider, public_only=False, transport=client._transport)
        self.assertEqual(await client.get_issue(REPOSITORY, 1), {"body": CANARY})
        with self.assertRaises(GitHubAppError):
            await client.add_issue_comment(REPOSITORY, 1, CANARY)
        with self.assertRaises(GitHubAppError):
            await client.write_wiki_pages(
                REPOSITORY, {"Home.md": CANARY}, COMMIT, "Update",
                expected_wiki_repository=REPOSITORY,
            )
        self.assertEqual(len(self.requests), 1)

    async def test_mcp_denial_preserves_native_failure_and_never_returns_private_metadata(self):
        github = self.client({**PUBLIC, "visibility": "private", "private": True, "description": CANARY})
        async with Client(create_server(github)) as client:
            result = await client.call_tool("get_issue", {"repository": REPOSITORY, "issue_number": 1})
        self.assertTrue(result.is_error)
        self.assertNotIn(CANARY, str(result))
        self.assertFalse(result.structured_content["success"])
        self.assertEqual(result.structured_content["outcome"], "not_applied")

    def test_standalone_server_defaults_public_and_rejects_invalid_profile_configuration(self):
        environment = {
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI": "https://test.vault.azure.net/secrets/app-key",
        }
        with patch("issuelens_github_mcp.server.GitHubClient") as factory:
            for flag, expected in ((None, True), ("true", True), ("  TRUE  ", True), ("false", False)):
                with self.subTest(flag=flag):
                    values = environment if flag is None else {**environment, "GITHUB_MCP_PUBLIC_ONLY": flag}
                    build_server_from_environment(values)
                    self.assertEqual(factory.call_args.kwargs["public_only"], expected)
            for flag in ("expand", "", " ", "\t"):
                with self.subTest(flag=flag):
                    factory.reset_mock()
                    with self.assertRaisesRegex(ConfigurationError, "GITHUB_MCP_PUBLIC_ONLY"):
                        build_server_from_environment({**environment, "GITHUB_MCP_PUBLIC_ONLY": flag})
                    factory.assert_not_called()
