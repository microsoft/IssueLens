import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from mcp import Client


PACKAGE_ROOT = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp.auth import GitHubAppError, GitHubAppTokenProvider  # noqa: E402
from issuelens_github_mcp.config import GitHubAppConfig  # noqa: E402
from issuelens_github_mcp.github import GitHubClient  # noqa: E402
from issuelens_github_mcp.server import create_server  # noqa: E402
from issuelens_github_mcp.wiki import WikiError, WikiRepository  # noqa: E402


REPOSITORY = "microsoft/IssueLens"
BASE = "a" * 40
HEAD = "b" * 40
TOKEN = "installation-test-token"
IDENTITY = ("issuelens[bot]", "7654321+issuelens[bot]@users.noreply.github.com")


class WikiClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = SimpleNamespace(
            get_token=AsyncMock(return_value=SimpleNamespace(token=TOKEN)),
            get_bot_identity=AsyncMock(return_value=IDENTITY),
        )
        self.github = GitHubClient(self.provider)

    def writer(self, **kwargs):
        return GitHubClient(
            self.provider, wiki_write_repositories=[REPOSITORY], **kwargs
        )

    async def test_all_reads_run_context_and_operation_in_one_worker_thread(self):
        reads = (
            ("get_wiki_snapshot", (), "snapshot", ()),
            ("list_wiki_pages", (BASE,), "pages", (BASE,)),
            ("get_wiki_page", ("Home.md", BASE), "page", ("Home.md", BASE)),
            ("search_wiki", ("text", BASE), "search", ("text", BASE)),
            ("list_wiki_history", (), "history", (None, 30, "HEAD")),
            ("list_wiki_history", ("Home.md", 5, BASE), "history", ("Home.md", 5, BASE)),
            ("get_wiki_diff", (BASE, HEAD), "diff", (BASE, HEAD)),
        )
        main_thread = threading.get_ident()
        for method, arguments, operation, expected in reads:
            with self.subTest(method=method, arguments=arguments):
                events = []
                wiki = MagicMock(spec=WikiRepository)

                def record(name, result):
                    events.append((name, threading.get_ident()))
                    return result

                wiki.__enter__.side_effect = lambda: record("enter", wiki)
                wiki.__exit__.side_effect = lambda *args: record("exit", None)
                getattr(wiki, operation).side_effect = lambda *args: record("operation", {"sha": BASE})
                with patch("issuelens_github_mcp.github.WikiRepository") as backend:
                    backend.side_effect = lambda *args, **kwargs: record("init", wiki)
                    result = await getattr(self.github, method)(REPOSITORY, *arguments)
                self.assertEqual(result, {"sha": BASE})
                backend.assert_called_once_with(REPOSITORY, token=TOKEN)
                getattr(wiki, operation).assert_called_once_with(*expected)
                self.assertEqual([name for name, _ in events], ["init", "enter", "operation", "exit"])
                self.assertEqual(len({thread for _, thread in events}), 1)
                self.assertNotEqual(events[0][1], main_thread)
                self.provider.get_token.assert_awaited_with(REPOSITORY, {"contents": "read"})
                self.provider.get_bot_identity.assert_not_awaited()

    async def test_wiki_writes_fail_closed_independently_of_issue_writes(self):
        for issue_writes in (False, True):
            client = GitHubClient(self.provider, writes_enabled=issue_writes)
            self.assertFalse(client.wiki_writes_enabled)
            with self.assertRaisesRegex(GitHubAppError, "not enabled"):
                await client.write_wiki_pages(REPOSITORY, {"Home.md": "text"}, BASE, "Update")
        self.provider.get_token.assert_not_awaited()
        self.provider.get_bot_identity.assert_not_awaited()

    async def test_allowlist_requires_exact_membership_before_authentication(self):
        writer = self.writer()
        for repository in ("microsoft/other", "other/IssueLens", "microsoft/IssueLens-other", "IssueLens"):
            with self.subTest(repository=repository):
                with self.assertRaises(GitHubAppError):
                    await writer.write_wiki_pages(repository, {"Home.md": "text"}, BASE, "Update")
        self.provider.get_token.assert_not_awaited()
        self.provider.get_bot_identity.assert_not_awaited()

    def test_constructor_rejects_invalid_or_duplicate_allowlists(self):
        for repositories in (
            REPOSITORY, None, [None], [""], ["invalid"], ["owner/.."],
            ["owner/repo,owner/other"], ["owner/*"], ["https://github.com/owner/repo"],
            [REPOSITORY, REPOSITORY.upper()],
        ):
            with self.subTest(repositories=repositories):
                with self.assertRaises(GitHubAppError):
                    GitHubClient(self.provider, wiki_write_repositories=repositories)
        self.assertTrue(GitHubClient(
            self.provider, wiki_write_repositories=[" microsoft/IssueLens "]
        ).wiki_writes_enabled)

    async def test_write_uses_contents_only_and_verified_identity_in_worker(self):
        writer = self.writer()
        self.assertTrue(writer.wiki_writes_enabled)
        self.assertFalse(writer.writes_enabled)
        expected = {"status": "updated", "sha": HEAD, "branch": "master", "pages": ["Home.md"], "repository": REPOSITORY}
        threads = []
        wiki = MagicMock(spec=WikiRepository)
        wiki.__enter__.side_effect = lambda: threads.append(threading.get_ident()) or wiki
        wiki.__exit__.side_effect = lambda *args: threads.append(threading.get_ident())
        wiki.write.side_effect = lambda *args, **kwargs: threads.append(threading.get_ident()) or expected
        with patch("issuelens_github_mcp.github.WikiRepository", return_value=wiki) as backend:
            result = await writer.write_wiki_pages(REPOSITORY.upper(), {"Home.md": "text"}, BASE, "Update")
        self.assertEqual(result, expected)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.provider.get_token.assert_awaited_once_with(REPOSITORY.upper(), {"contents": "write"})
        self.provider.get_bot_identity.assert_awaited_once_with()
        backend.assert_called_once_with(REPOSITORY.upper(), token=TOKEN)
        wiki.write.assert_called_once_with(
            {"Home.md": "text"}, BASE, "Update", author_name=IDENTITY[0], author_email=IDENTITY[1]
        )
        self.assertEqual(len(threads), 3)
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    async def test_authentication_failures_do_not_open_backend_or_expose_secrets(self):
        for operation in ("get_token", "get_bot_identity"):
            with self.subTest(operation=operation):
                failing = getattr(self.provider, operation)
                failing.side_effect = RuntimeError(TOKEN)
                with patch("issuelens_github_mcp.github.WikiRepository") as backend:
                    with self.assertRaisesRegex(GitHubAppError, "authentication failed") as caught:
                        await self.writer().write_wiki_pages(REPOSITORY, {"Home.md": "text"}, BASE, "Update")
                    backend.assert_not_called()
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                failing.side_effect = None

    async def test_read_authentication_failure_is_sanitized(self):
        self.provider.get_token.side_effect = GitHubAppError(TOKEN)
        with patch("issuelens_github_mcp.github.WikiRepository") as backend:
            with self.assertRaisesRegex(GitHubAppError, "read authentication failed"):
                await self.github.get_wiki_snapshot(REPOSITORY)
            backend.assert_not_called()

    async def test_backend_errors_close_context_and_never_expose_credentials(self):
        for failure in (WikiError(TOKEN), RuntimeError(TOKEN)):
            for operation in ("snapshot", "write", "__enter__", "__exit__"):
                with self.subTest(failure=type(failure), operation=operation):
                    wiki = MagicMock(spec=WikiRepository)
                    wiki.__enter__.return_value = wiki
                    getattr(wiki, operation).side_effect = failure
                    with patch("issuelens_github_mcp.github.WikiRepository", return_value=wiki):
                        with self.assertRaises(GitHubAppError) as caught:
                            if operation == "snapshot":
                                await self.github.get_wiki_snapshot(REPOSITORY)
                            else:
                                await self.writer().write_wiki_pages(REPOSITORY, {"Home.md": "text"}, BASE, "Update")
                    self.assertNotIn(TOKEN, str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    if operation != "__enter__":
                        wiki.__exit__.assert_called_once()

    async def test_structured_result_budget_and_credential_boundary_apply_to_reads_and_writes(self):
        for payload in ({"content": "\u00e9" * 70_000}, {"token": TOKEN}, {"invalid": object()}, {"invalid": float("nan")}):
            for write in (False, True):
                with self.subTest(payload_type=next(iter(payload)), write=write):
                    wiki = MagicMock(spec=WikiRepository)
                    wiki.__enter__.return_value = wiki
                    wiki.snapshot.return_value = wiki.write.return_value = payload
                    with patch("issuelens_github_mcp.github.WikiRepository", return_value=wiki):
                        with self.assertRaises(GitHubAppError) as caught:
                            if write:
                                await self.writer().write_wiki_pages(REPOSITORY, {"Home.md": "text"}, BASE, "Update")
                            else:
                                await self.github.get_wiki_snapshot(REPOSITORY)
                    self.assertNotIn(TOKEN, str(caught.exception))
                    wiki.__exit__.assert_called_once()

    async def test_real_backend_rejects_invalid_write_inputs_before_remote_refresh(self):
        invalid = (
            ({}, BASE, "Update"),
            ({f"Page-{index}.md": "text" for index in range(21)}, BASE, "Update"),
            ({"../Home.md": "text"}, BASE, "Update"),
            ({"Home.md": "x" * (64 * 1024 + 1)}, BASE, "Update"),
            ({f"Page-{index}.md": "x" * (64 * 1024) for index in range(5)}, BASE, "Update"),
            ({"Home.md": "text"}, "HEAD", "Update"),
            ({"Home.md": "text"}, "--output=other", "Update"),
            ({"Home.md": "text"}, BASE, "x" * 513),
            ({"Home.md": "text"}, BASE, "Invalid\nmessage"),
        )
        for pages, base, message in invalid:
            with self.subTest(base=base, page_count=len(pages), message_length=len(message)):
                with (
                    patch.object(WikiRepository, "__enter__", lambda wiki: wiki),
                    patch.object(WikiRepository, "__exit__", return_value=None),
                    patch.object(WikiRepository, "_refresh") as refresh,
                ):
                    with self.assertRaises(GitHubAppError):
                        await self.writer().write_wiki_pages(REPOSITORY, pages, base, message)
                    refresh.assert_not_called()


class WikiMCPRoundTripTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wiki-mcp-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = pathlib.Path(temporary.name)
        self.remote = self.directory / "fixture.git"
        self.remote.mkdir()
        self.environment = WikiRepository(REPOSITORY)._environment(self.directory)
        self.environment.pop("GIT_INDEX_FILE")
        self.environment.update({
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
        })
        self.git("init", "--bare", "--initial-branch=docs/wiki")
        blob = self.git("hash-object", "-w", "--stdin", input_bytes=b"# Home\nWelcome\n").strip()
        tree = self.git("mktree", input_bytes=f"100644 blob {blob}\tHome.md\n".encode()).strip()
        self.base = self.git("commit-tree", tree, input_bytes=b"Seed\n").strip()
        self.git("update-ref", "refs/heads/docs/wiki", self.base)
        remote = self.remote

        class LocalWiki(WikiRepository):
            @property
            def remote(self):
                return str(remote)

            def _environment(self, parent):
                environment = super()._environment(parent)
                environment["GIT_ALLOW_PROTOCOL"] = "file"
                return environment

        self.local_wiki = LocalWiki
        self.requests = []

        def handler(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, "api.github.com")
            if request.url.path == f"/repos/{REPOSITORY}/installation":
                return httpx.Response(200, json={"id": 1234})
            if request.url.path == "/app/installations/1234/access_tokens":
                return httpx.Response(201, json={"token": TOKEN, "expires_at": "2030-01-01T01:00:00Z"})
            if request.url.path == "/app":
                return httpx.Response(200, json={"id": 1816975, "slug": "issuelens"})
            if request.url.path == "/users/issuelens[bot]":
                return httpx.Response(200, json={"id": 7654321, "login": IDENTITY[0], "type": "Bot"})
            raise AssertionError("Unexpected API request")

        self.provider = GitHubAppTokenProvider(
            GitHubAppConfig("1816975", "https://issuelens.vault.azure.net/secrets/not-read"),
            private_key_loader=AsyncMock(return_value="mocked-key"),
            transport=httpx.MockTransport(handler),
        )

    def git(self, *arguments, input_bytes=None):
        return subprocess.run(
            ["git", "-C", str(self.remote), *arguments], input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            timeout=15, env=self.environment, shell=False,
        ).stdout.decode("utf-8")

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="mocked-app-jwt")
    async def test_maximum_utf8_page_can_be_read_after_writing(self, _):
        server = create_server(GitHubClient(
            self.provider, wiki_write_repositories=[REPOSITORY]
        ))
        content = "\u00e9" * (32 * 1024)
        with patch("issuelens_github_mcp.github.WikiRepository", self.local_wiki):
            async with Client(server) as client:
                written = await client.call_tool("write_wiki_pages", {
                    "repository": REPOSITORY,
                    "pages": {"Unicode.md": content},
                    "expected_base": self.base,
                    "message": "Document Unicode content",
                })
                self.assertFalse(written.is_error, written.content)
                snapshot = json.loads(written.content[0].text)["sha"]
                result = await client.call_tool("get_wiki_page", {
                    "repository": REPOSITORY, "path": "Unicode.md", "ref": snapshot,
                })
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(json.loads(result.content[0].text)["content"], content)

    @patch("issuelens_github_mcp.auth.jwt.encode", return_value="mocked-app-jwt")
    async def test_real_mcp_write_changes_local_sha_and_supports_pinned_reads(self, _):
        github = GitHubClient(self.provider, wiki_write_repositories=[REPOSITORY])
        server = create_server(github)
        write = {
            "repository": REPOSITORY, "pages": {"Home.md": "# Home\nUpdated memory\n", "New.md": "New page\n"},
            "expected_base": self.base, "message": "Update wiki memory",
        }

        async def call_json(client, name, arguments, *, sequence=False, text=False):
            result = await client.call_tool(name, arguments)
            self.assertFalse(result.is_error, result.content)
            for item in result.content:
                self.assertNotIn(TOKEN, item.text)
                self.assertNotIn("mocked-app-jwt", item.text)
            if sequence:
                return [item.text if text else json.loads(item.text) for item in result.content]
            return json.loads(result.content[0].text)

        with patch("issuelens_github_mcp.github.WikiRepository", self.local_wiki):
            async with Client(server) as client:
                before = await call_json(client, "get_wiki_snapshot", {"repository": REPOSITORY})
                self.assertEqual(before["sha"], self.base)
                updated = await call_json(client, "write_wiki_pages", write)
                self.assertEqual(set(updated), {"status", "sha", "branch", "pages", "repository"})
                self.assertEqual(updated["status"], "updated")
                self.assertEqual(updated["branch"], "docs/wiki")
                self.assertNotEqual(updated["sha"], before["sha"])
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), updated["sha"])
                self.assertEqual(self.git("rev-parse", "HEAD^").strip(), self.base)
                self.assertEqual(self.git("show", "-s", "--format=%an|%ae|%cn|%ce", "HEAD").strip(), "|".join(IDENTITY * 2))
                page = await call_json(client, "get_wiki_page", {"repository": REPOSITORY, "path": "Home.md"})
                self.assertEqual(page["content"], write["pages"]["Home.md"])
                old_page = await call_json(client, "get_wiki_page", {"repository": REPOSITORY, "path": "Home.md", "ref": self.base})
                self.assertEqual(old_page["content"], "# Home\nWelcome\n")
                history = await call_json(client, "list_wiki_history", {"repository": REPOSITORY, "ref": self.base}, sequence=True, text=True)
                self.assertEqual(history, [self.base])
                pages = await call_json(client, "list_wiki_pages", {"repository": REPOSITORY, "ref": updated["sha"]}, sequence=True)
                self.assertEqual([page["path"] for page in pages], ["Home.md", "New.md"])
                found = await call_json(client, "search_wiki", {"repository": REPOSITORY, "query": "Updated", "ref": updated["sha"]}, sequence=True)
                self.assertEqual([page["path"] for page in found], ["Home.md"])
                diff = await client.call_tool("get_wiki_diff", {"repository": REPOSITORY, "base": self.base, "head": updated["sha"]})
                self.assertFalse(diff.is_error)
                self.assertIn("+Updated memory", diff.content[0].text)
                retry = await call_json(client, "write_wiki_pages", write)
                self.assertEqual(retry["status"], "no-change")
                self.assertEqual(retry["sha"], updated["sha"])
                for invalid in (
                    {**write, "pages": {"Home.md": "Conflicting content"}},
                    {**write, "pages": {"../escape.md": "Invalid"}},
                    {**write, "expected_base": "HEAD"},
                ):
                    result = await client.call_tool("write_wiki_pages", invalid)
                    self.assertTrue(result.is_error)
                    self.assertNotIn(TOKEN, str(result.content))
                    self.assertEqual(self.git("rev-parse", "HEAD").strip(), updated["sha"])
                requests_before_denial = len(self.requests)
                denied = await client.call_tool("write_wiki_pages", {**write, "repository": "microsoft/other"})
                self.assertTrue(denied.is_error)
                self.assertEqual(len(self.requests), requests_before_denial)

        token_requests = [json.loads(request.content) for request in self.requests if request.url.path.endswith("/access_tokens")]
        self.assertEqual(token_requests, [
            {"repositories": ["IssueLens"], "permissions": {"contents": "read"}},
            {"repositories": ["IssueLens"], "permissions": {"contents": "write"}},
        ])
        self.assertEqual(sum(request.url.path == "/app" for request in self.requests), 1)
        self.assertEqual(sum(request.url.path == "/users/issuelens[bot]" for request in self.requests), 1)


if __name__ == "__main__":
    unittest.main()