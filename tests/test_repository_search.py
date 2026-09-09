import asyncio
import base64
import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import AsyncMock, call, patch

import httpx

from github_app_mcp.src.issuelens_github_mcp.auth import GitHubAppError, GitHubAppTokenProvider, InstallationCredential
from github_app_mcp.src.issuelens_github_mcp.config import GitHubAppConfig
from github_app_mcp.src.issuelens_github_mcp import github as github_module
from github_app_mcp.src.issuelens_github_mcp.github import GitHubClient, _ContentReadAuth


REPOSITORY = "microsoft/IssueLens"
OLD_COMMIT = "a" * 40
NEW_COMMIT = "b" * 40
OLD_TREE = "c" * 40
NEW_TREE = "d" * 40


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.available = True

    async def get_token(self, repository, permissions):
        self.calls.append((repository, permissions))
        if not self.available:
            raise GitHubAppError("No App installation")
        return InstallationCredential(
            installation_id=1234,
            repository=repository,
            permissions=tuple(sorted(permissions.items())),
            token="fake-repository-token",
            expires_at=float("inf"),
        )


class BlockingBody(httpx.AsyncByteStream):
    def __init__(self, first_chunk=b'{"sha":'):
        self.first_chunk = first_chunk
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.first_chunk
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()

    async def aclose(self):
        self.closed = True


class RepositorySearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.provider = FakeProvider()
        self.overrides = {}
        self.advance_ref_on_resolve = False
        self.blobs = {}
        self.trees = {
            OLD_TREE: {"sha": OLD_TREE, "truncated": False, "tree": []},
            NEW_TREE: {"sha": NEW_TREE, "truncated": False, "tree": []},
        }
        self.refs = {"release/topic": OLD_COMMIT, "main": NEW_COMMIT}
        self.commits = {
            OLD_COMMIT: {"sha": OLD_COMMIT, "commit": {"tree": {"sha": OLD_TREE}}},
            NEW_COMMIT: {"sha": NEW_COMMIT, "commit": {"tree": {"sha": NEW_TREE}}},
        }
        self.indexed = {"items": [{"path": "current-main.txt"}], "total_count": 1}
        self.client = GitHubClient(
            self.provider, transport=httpx.MockTransport(self.handler)
        )

    def track_clients(self):
        clients = []
        original_client = httpx.AsyncClient

        def create_client(**kwargs):
            client = original_client(**kwargs)
            client.aclose = AsyncMock(wraps=client.aclose)
            client.send = AsyncMock(wraps=client.send)
            clients.append(client)
            return client

        self.enterContext(patch.object(github_module.httpx, "AsyncClient", side_effect=create_client))
        return clients

    def assert_clients_closed(self, clients):
        for client in clients:
            client.aclose.assert_awaited_once_with()
            self.assertTrue(client.is_closed)

    def add_file(self, path, content, *, tree=OLD_TREE, mode="100644"):
        blob_sha = hashlib.sha1(
            b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
        ).hexdigest()
        self.trees[tree]["tree"].append({
            "path": path, "type": "blob", "mode": mode,
            "sha": blob_sha, "size": len(content),
        })
        self.blobs[blob_sha] = {
            "sha": blob_sha, "size": len(content), "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }
        return blob_sha

    def handler(self, request):
        self.requests.append(request)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "api.github.com")
        if request.url.path == "/search/code":
            return httpx.Response(200, json=self.indexed)
        prefix = f"/repos/{REPOSITORY}/"
        self.assertTrue(request.url.path.casefold().startswith(prefix.casefold()))
        route = request.url.path[len(prefix):]
        if route in self.overrides:
            if isinstance(self.overrides[route], Exception):
                raise self.overrides[route]
            return self.overrides[route]
        if route.startswith("commits/"):
            ref = route.removeprefix("commits/")
            commit = self.commits.get(self.refs.get(ref, ref).lower())
            if self.advance_ref_on_resolve:
                self.refs["release/topic"] = NEW_COMMIT
            return httpx.Response(200, json=commit) if commit is not None else httpx.Response(404)
        if route.startswith("git/trees/"):
            self.assertEqual(request.url.params["recursive"], "1")
            return httpx.Response(200, json=self.trees[route.removeprefix("git/trees/")])
        if route.startswith("git/blobs/"):
            return httpx.Response(200, json=self.blobs[route.removeprefix("git/blobs/")])
        self.fail(f"Unexpected request: {request.url}")

    def use_real_provider(self, *, installed):
        self.installation_available = installed
        self.auth_requests = []

        def handler(request):
            self.auth_requests.append(request)
            self.assertEqual(request.url.host, "api.github.com")
            self.assertEqual(request.headers["Authorization"], "Bearer fake-app-jwt")
            if request.url.path == f"/repos/{REPOSITORY}/installation":
                self.assertEqual(request.method, "GET")
                return httpx.Response(200, json={"id": 1234}) if self.installation_available else httpx.Response(404)
            self.assertEqual(request.url.path, "/app/installations/1234/access_tokens")
            self.assertEqual(request.method, "POST")
            self.assertTrue(self.installation_available)
            self.assertEqual(json.loads(request.content), {
                "repositories": ["IssueLens"], "permissions": {"contents": "read"},
            })
            return httpx.Response(201, json={
                "token": "fake-repository-token", "expires_at": "2030-01-01T01:00:00Z",
            })

        self.provider = GitHubAppTokenProvider(
            GitHubAppConfig("1234", "https://unused.vault.azure.net/secrets/unused"),
            private_key_loader=AsyncMock(side_effect=AssertionError("Unexpected secret read")),
            transport=httpx.MockTransport(handler),
            clock=lambda: 1_700_000_000,
        )
        self.enterContext(patch.object(self.provider, "_app_jwt", new=AsyncMock(return_value="fake-app-jwt")))
        lookup = self.enterContext(patch.object(self.provider, "get_token", wraps=self.provider.get_token))
        self.client = GitHubClient(self.provider, transport=httpx.MockTransport(self.handler))
        return lookup

    async def test_scan_deadline_includes_authentication(self):
        clients = self.track_clients()
        blocked = asyncio.Event()
        cancelled = asyncio.Event()

        async def get_token(repository, permissions):
            try:
                await blocked.wait()
            finally:
                cancelled.set()

        with patch.object(self.provider, "get_token", side_effect=get_token) as lookup:
            with patch.object(github_module, "_MAX_SEARCH_SECONDS", 0.05):
                with self.assertRaisesRegex(GitHubAppError, "scan time budget"):
                    await asyncio.wait_for(
                        self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT),
                        timeout=2,
                    )
        self.assertTrue(cancelled.is_set())
        lookup.assert_awaited_once_with(REPOSITORY, {"contents": "read"})
        self.assertEqual(self.requests, [])
        self.assertEqual(len(clients), 1)
        self.assert_clients_closed(clients)

        result = await asyncio.wait_for(
            self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT),
            timeout=2,
        )
        self.assertEqual(result["total_count"], 0)
        self.assertFalse(result["incomplete_results"])
        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0], clients[1])
        self.assert_clients_closed(clients)

    async def test_deadline_cancels_underlying_auth_http_and_body_reads(self):
        clients = self.track_clients()
        for stage in ("installation", "mint", "body"):
            with self.subTest(stage=stage):
                self.requests.clear()
                auth_requests = []
                stream = BlockingBody()

                async def handler(request):
                    auth_requests.append(request)
                    self.assertEqual(request.url.host, "api.github.com")
                    self.assertEqual(request.headers["Authorization"], "Bearer fake-app-jwt")
                    if request.url.path == f"/repos/{REPOSITORY}/installation" and stage != "installation":
                        return httpx.Response(200, json={"id": 1234})
                    if stage == "body":
                        return httpx.Response(201, stream=stream)
                    stream.started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stream.cancelled.set()

                provider = GitHubAppTokenProvider(
                    GitHubAppConfig("1234", "https://unused.vault.azure.net/secrets/unused"),
                    private_key_loader=AsyncMock(side_effect=AssertionError("Unexpected secret read")),
                    transport=httpx.MockTransport(handler),
                    clock=lambda: 1_700_000_000,
                )
                client = GitHubClient(provider, transport=httpx.MockTransport(self.handler))
                before = len(clients)
                with patch.object(provider, "_app_jwt", new=AsyncMock(return_value="fake-app-jwt")):
                    with patch.object(github_module, "_MAX_SEARCH_SECONDS", 0.05):
                        with self.assertRaisesRegex(GitHubAppError, "scan time budget"):
                            await asyncio.wait_for(
                                client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT),
                                timeout=2,
                            )
                self.assertTrue(stream.started.is_set())
                self.assertTrue(stream.cancelled.is_set())
                self.assertEqual(len(auth_requests), 1 if stage == "installation" else 2)
                self.assertEqual(self.requests, [])
                self.assertTrue(all(http_client.is_closed for http_client in clients[before:]))
                clients[before].aclose.assert_awaited_once_with()
                if stage == "body":
                    self.assertTrue(stream.closed)

    async def test_deadline_cancels_each_content_stage_and_next_scan_recovers(self):
        self.add_file("a.txt", b"needle already matched")
        last_blob = self.add_file("z.txt", b"needle last file")
        clients = self.track_clients()
        routes = {
            "commit": (f"commits/{OLD_COMMIT}", 1),
            "tree": (f"git/trees/{OLD_TREE}", 2),
            "blob": (f"git/blobs/{last_blob}", 4),
            "body": (f"git/blobs/{last_blob}", 4),
        }
        for stage, (route, request_count) in routes.items():
            with self.subTest(stage=stage):
                self.requests.clear()
                self.provider.calls.clear()
                stream = BlockingBody()
                started = stream.started
                cancelled = stream.cancelled
                recovered = False

                async def handler(request):
                    response = self.handler(request)
                    if not recovered and request.url.path.endswith(f"/{route}"):
                        if stage == "body":
                            return httpx.Response(200, stream=stream)
                        started.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            cancelled.set()
                    return response

                client = GitHubClient(self.provider, transport=httpx.MockTransport(handler))
                before = len(clients)
                with patch.object(github_module, "_MAX_SEARCH_SECONDS", 0.05):
                    with self.assertRaisesRegex(GitHubAppError, "scan time budget"):
                        await asyncio.wait_for(
                            client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT),
                            timeout=2,
                        )
                self.assertTrue(started.is_set())
                self.assertTrue(cancelled.is_set())
                self.assertEqual(len(self.requests), request_count)
                self.assertEqual(self.provider.calls, [(REPOSITORY, {"contents": "read"})])
                self.assertEqual(len(clients), before + 1)
                self.assert_clients_closed(clients)
                if stage == "body":
                    self.assertTrue(stream.closed)

                recovered = True
                self.requests.clear()
                result = await asyncio.wait_for(
                    client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT),
                    timeout=2,
                )
                self.assertEqual(result["total_count"], 2)
                self.assertFalse(result["incomplete_results"])
                self.assertEqual(len(self.requests), 4)
                self.assertEqual(len(self.provider.calls), 2)
                self.assertEqual(len(clients), before + 2)
                self.assertIsNot(clients[-1], clients[-2])
                self.assert_clients_closed(clients)

    async def test_host_cancellation_propagates_and_closes_each_scan_stage(self):
        self.add_file("a.txt", b"needle already matched")
        last_blob = self.add_file("z.txt", b"needle last file")
        clients = self.track_clients()
        original_get_token = self.provider.get_token
        routes = {
            "auth": (None, 0),
            "commit": (f"commits/{OLD_COMMIT}", 1),
            "tree": (f"git/trees/{OLD_TREE}", 2),
            "blob": (f"git/blobs/{last_blob}", 4),
            "body": (f"git/blobs/{last_blob}", 4),
        }
        for stage, (route, request_count) in routes.items():
            with self.subTest(stage=stage):
                self.requests.clear()
                stream = BlockingBody()

                async def block():
                    stream.started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stream.cancelled.set()

                async def get_token(repository, permissions):
                    if stage == "auth":
                        await block()
                    return await original_get_token(repository, permissions)

                async def handler(request):
                    response = self.handler(request)
                    if route and request.url.path.endswith(f"/{route}"):
                        if stage == "body":
                            return httpx.Response(200, stream=stream)
                        await block()
                    return response

                client = GitHubClient(self.provider, transport=httpx.MockTransport(handler))
                before = len(clients)
                with patch.object(self.provider, "get_token", side_effect=get_token) as lookup:
                    task = asyncio.create_task(
                        client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                    )
                    try:
                        await asyncio.wait_for(stream.started.wait(), timeout=2)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, timeout=2)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    lookup.assert_awaited_once_with(REPOSITORY, {"contents": "read"})
                self.assertTrue(stream.cancelled.is_set())
                self.assertEqual(len(self.requests), request_count)
                self.assertEqual(len(clients), before + 1)
                self.assert_clients_closed(clients)
                if stage == "body":
                    self.assertTrue(stream.closed)

    async def test_one_deadline_covers_auth_requests_and_local_results(self):
        for index in range(64):
            self.add_file(f"{index:02}.txt", b"needle")
        budget = asyncio.timeout(60)
        deadlines = []
        original_get_token = self.provider.get_token
        original_matches = github_module._search_line_matches

        async def get_token(repository, permissions):
            deadlines.append(budget.when())
            return await original_get_token(repository, permissions)

        def handler(request):
            deadlines.append(budget.when())
            return self.handler(request)

        def matches(text, query):
            deadlines.append(budget.when())
            return original_matches(text, query)

        self.client = GitHubClient(self.provider, transport=httpx.MockTransport(handler))
        with patch.object(github_module.asyncio, "timeout", return_value=budget) as timeout:
            with patch.object(self.provider, "get_token", side_effect=get_token):
                with patch.object(github_module, "_search_line_matches", side_effect=matches):
                    result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        timeout.assert_called_once_with(60)
        self.assertEqual(len(deadlines), 1 + 66 + 64)
        self.assertEqual(set(deadlines), {budget.when()})
        self.assertFalse(budget.expired())
        self.assertEqual(result["total_count"], 64)

    async def test_expired_budget_rejects_locally_constructed_result(self):
        self.add_file("file.txt", b"needle")
        clients = self.track_clients()
        budget = asyncio.timeout(60)
        original_matches = github_module._search_line_matches

        def matches(text, query):
            budget.reschedule(asyncio.get_running_loop().time() - 1)
            return original_matches(text, query)

        with patch.object(github_module.asyncio, "timeout", return_value=budget):
            with patch.object(github_module, "_search_line_matches", side_effect=matches):
                with self.assertRaisesRegex(GitHubAppError, "scan time budget"):
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(clients), 1)
        self.assert_clients_closed(clients)

    async def test_unrelated_timeouts_are_not_reported_as_scan_deadline(self):
        clients = self.track_clients()
        for stage in ("auth", "request"):
            with self.subTest(stage=stage):
                failure = TimeoutError("upstream timeout")
                if stage == "auth":
                    with patch.object(self.provider, "get_token", side_effect=failure):
                        with self.assertRaises(TimeoutError) as raised:
                            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                else:
                    self.overrides[f"commits/{OLD_COMMIT}"] = failure
                    with self.assertRaises(TimeoutError) as raised:
                        await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertIs(raised.exception, failure)
                self.assert_clients_closed(clients)
        self.assertEqual(len(clients), 2)
        self.assertEqual(len(self.requests), 1)

    async def test_borrowed_client_requires_scoped_auth_and_is_not_closed_per_request(self):
        clients = self.track_clients()
        borrowed = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        credential = await self.provider.get_token(REPOSITORY, {"contents": "read"})
        self.provider.calls.clear()
        context = _ContentReadAuth(REPOSITORY, credential)
        try:
            with self.assertRaisesRegex(GitHubAppError, "scope mismatch"):
                await self.client._request(
                    "GET", REPOSITORY, permissions={"contents": "read"}, _client=borrowed,
                )
            with self.assertRaisesRegex(GitHubAppError, "scope mismatch"):
                await self.client._request(
                    "GET", REPOSITORY, permissions={"contents": "read"},
                    absolute_url="https://other.example/", content_read_auth=context, _client=borrowed,
                )
            with self.assertRaisesRegex(GitHubAppError, "write tools are disabled"):
                await self.client._request(
                    "POST", REPOSITORY, permissions={"contents": "write"}, write=True,
                    content_read_auth=context, _client=borrowed,
                )
            self.assertEqual(self.requests, [])
            for route, params in (
                (f"commits/{OLD_COMMIT}", None),
                (f"git/trees/{OLD_TREE}", {"recursive": "1"}),
            ):
                await self.client._request(
                    "GET", REPOSITORY, f"/{route}", permissions={"contents": "read"},
                    params=params, content_read_auth=context, _client=borrowed,
                )
                borrowed.aclose.assert_not_awaited()
                self.assertFalse(borrowed.is_closed)
            self.assertEqual(self.provider.calls, [])
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(len(clients), 1)
        finally:
            await borrowed.aclose()
        self.assert_clients_closed(clients)

    async def test_streamed_response_limit_closes_owned_and_scan_clients(self):
        clients = self.track_clients()
        for ref in (None, OLD_COMMIT):
            with self.subTest(ref=ref):
                stream = BlockingBody(b"x" * (128 * 1024 + 1))
                client = GitHubClient(
                    self.provider,
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
                )
                with self.assertRaisesRegex(GitHubAppError, "response is too large"):
                    await asyncio.wait_for(
                        client.search_repository_content(REPOSITORY, "needle", ref=ref),
                        timeout=2,
                    )
                self.assertFalse(stream.started.is_set())
                self.assertTrue(stream.closed)
                self.assert_clients_closed(clients)
        self.assertEqual(len(clients), 2)

    async def test_httpx_timeout_keeps_safe_error_and_closes_clients(self):
        clients = self.track_clients()

        def handler(request):
            self.requests.append(request)
            raise httpx.ReadTimeout("secret-response-sentinel")

        client = GitHubClient(self.provider, transport=httpx.MockTransport(handler))
        for ref in (None, OLD_COMMIT):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(GitHubAppError, "^GitHub API request failed$"):
                    await client.search_repository_content(REPOSITORY, "needle", ref=ref)
                self.assert_clients_closed(clients)
        self.assertEqual(len(clients), 2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.provider.calls), 2)

    async def test_concurrent_scans_of_same_repository_own_clients_and_auth(self):
        self.add_file("file.txt", b"needle")
        clients = self.track_clients()
        both_started = asyncio.Event()
        started_count = 0

        async def handler(request):
            nonlocal started_count
            if request.url.path.endswith(f"/commits/{OLD_COMMIT}"):
                started_count += 1
                if started_count == 2:
                    both_started.set()
                await both_started.wait()
            return self.handler(request)

        client = GitHubClient(self.provider, transport=httpx.MockTransport(handler))
        results = await asyncio.wait_for(asyncio.gather(
            client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT),
            client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT),
        ), timeout=2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["total_count"], 1)
        self.assertEqual(self.provider.calls, [(REPOSITORY, {"contents": "read"})] * 2)
        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0], clients[1])
        self.assertEqual([client.send.await_count for client in clients], [3, 3])
        self.assert_clients_closed(clients)

    async def test_ref_search_uses_only_requested_snapshot(self):
        old_blob = self.add_file("src/old.py", b"old implementation\nNEEDLE from release\n")
        self.add_file("src/new.py", b"needle from current main", tree=NEW_TREE)
        self.advance_ref_on_resolve = True

        result = await self.client.search_repository_content(
            REPOSITORY, "needle", ref="release/topic"
        )

        self.assertEqual(result["resolved_ref"], OLD_COMMIT)
        self.assertEqual(result["total_count"], 1)
        self.assertFalse(result["incomplete_results"])
        self.assertEqual(result["skipped_files"], 0)
        self.assertEqual([item["path"] for item in result["items"]], ["src/old.py"])
        self.assertEqual(result["items"][0]["sha"], old_blob)
        self.assertEqual(result["items"][0]["repository"], {"full_name": REPOSITORY})
        self.assertEqual(
            result["items"][0]["html_url"],
            f"https://github.com/{REPOSITORY}/blob/{OLD_COMMIT}/src/old.py",
        )
        self.assertEqual(result["items"][0]["matches"][0]["line_number"], 2)
        self.assertEqual(
            [request.url.path for request in self.requests],
            [f"/repos/{REPOSITORY}/commits/release/topic",
             f"/repos/{REPOSITORY}/git/trees/{OLD_TREE}",
             f"/repos/{REPOSITORY}/git/blobs/{old_blob}"],
        )
        self.assertIn(b"release%2Ftopic", self.requests[0].url.raw_path)
        self.assertTrue(all(
            call == (REPOSITORY, {"contents": "read"}) for call in self.provider.calls
        ))

    async def test_no_ref_preserves_indexed_query(self):
        clients = self.track_clients()
        with patch.object(github_module.asyncio, "timeout", side_effect=AssertionError("Indexed search has no scan deadline")):
            result = await self.client.search_repository_content(
                REPOSITORY, "needle", per_page=5, page=2
            )

        self.assertEqual(result, self.indexed)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/search/code")
        self.assertEqual(self.requests[0].url.params["q"], f"needle repo:{REPOSITORY}")
        self.assertEqual(self.requests[0].url.params["per_page"], "5")
        self.assertEqual(self.requests[0].url.params["page"], "2")
        self.assertEqual(self.provider.calls, [(REPOSITORY, {"contents": "read"})])
        self.assertEqual(len(clients), 1)
        self.assert_clients_closed(clients)
        self.assertEqual(self.requests[0].extensions["timeout"], {
            "connect": 30, "read": 30, "write": 30, "pool": 30,
        })

    async def test_invalid_queries_are_rejected_before_authentication(self):
        queries = (
            None, 123, [], {}, "", " " * 10, "a" * 513,
            "needle repo:other/project", "org:other", "path:secret", "ref:main",
            "needle\n", "needle\t", "needle\x00", "needle\x7f",
            "needle\u200b", "needle\u0085", "needle\u2028",
        )
        for ref in (None, "main"):
            for query in queries:
                with self.subTest(query=query, ref=ref):
                    with self.assertRaises(GitHubAppError):
                        await self.client.search_repository_content(REPOSITORY, query, ref=ref)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.provider.calls, [])

    async def test_invalid_refs_and_repository_are_rejected_before_authentication(self):
        for ref in ("", 123, [], "main\n", "main:other", "../main", "HEAD~1", "-main", "main?repo=other"):
            with self.subTest(ref=ref):
                with self.assertRaises(GitHubAppError):
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=ref)
        with self.assertRaises(GitHubAppError):
            await self.client.search_repository_content("microsoft/IssueLens/other", "needle", ref="main")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.provider.calls, [])

    async def test_invalid_pagination_is_rejected_before_authentication(self):
        for ref in (None, "main"):
            for pagination in (
                {"page": 0}, {"page": 101}, {"page": True}, {"page": "1"},
                {"per_page": 0}, {"per_page": 101}, {"per_page": False},
            ):
                with self.subTest(ref=ref, pagination=pagination):
                    with self.assertRaises(GitHubAppError):
                        await self.client.search_repository_content(REPOSITORY, "needle", ref=ref, **pagination)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.provider.calls, [])

    async def test_pin_failures_never_fall_back_to_index(self):
        for status in (301, 403, 404, 422, 500):
            with self.subTest(status=status):
                self.requests.clear()
                self.overrides["commits/release/topic"] = httpx.Response(
                    status, text="secret-response-sentinel", headers={"Location": "https://other.example"}
                )
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref="release/topic")
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 1)
                self.assertNotEqual(self.requests[0].url.path, "/search/code")

    async def test_pagination_reuses_resolved_commit_after_branch_moves(self):
        self.add_file("z.txt", b"needle old last")
        self.add_file("a.txt", b"needle old first")
        self.add_file("new.txt", b"needle new snapshot", tree=NEW_TREE)
        first = await self.client.search_repository_content(REPOSITORY, "needle", ref="release/topic", per_page=1)
        self.refs["release/topic"] = NEW_COMMIT
        second = await self.client.search_repository_content(
            REPOSITORY, "needle", ref=first["resolved_ref"], per_page=1, page=2
        )
        repeat = await self.client.search_repository_content(
            REPOSITORY, "needle", ref=first["resolved_ref"], per_page=1
        )
        past_end = await self.client.search_repository_content(
            REPOSITORY, "needle", ref=first["resolved_ref"], per_page=1, page=3
        )
        moved = await self.client.search_repository_content(REPOSITORY, "needle", ref="release/topic")

        self.assertEqual(first, repeat)
        self.assertEqual(first["total_count"], 2)
        self.assertFalse(first["incomplete_results"])
        self.assertEqual(first["items"][0]["path"], "a.txt")
        self.assertEqual(second["items"][0]["path"], "z.txt")
        self.assertEqual(second["resolved_ref"], OLD_COMMIT)
        self.assertEqual(past_end["items"], [])
        self.assertEqual(past_end["total_count"], 2)
        self.assertEqual(moved["resolved_ref"], NEW_COMMIT)
        self.assertEqual([item["path"] for item in moved["items"]], ["new.txt"])

    async def test_literal_content_not_paths_regex_or_search_operators(self):
        self.add_file("needle.txt", b"unrelated content")
        self.add_file("literal.txt", "Stra\u00dfe a.*b OR literal\n".encode("utf-8"))
        for query in ("STRASSE", "a.*b OR literal"):
            with self.subTest(query=query):
                result = await self.client.search_repository_content(REPOSITORY, query, ref=OLD_COMMIT)
                self.assertEqual([item["path"] for item in result["items"]], ["literal.txt"])
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(result["total_count"], 0)
        self.assertFalse(result["incomplete_results"])

    async def test_case_preserving_branch_and_safe_commit_pinned_url(self):
        self.refs["Release/Topic"] = OLD_COMMIT
        path = "src/space #?% \u00fc.txt"
        blob_sha = self.add_file(path, b"needle", mode="100755")
        self.commits[OLD_COMMIT]["sha"] = OLD_COMMIT.upper()
        self.trees[OLD_TREE]["sha"] = OLD_TREE.upper()
        self.blobs[blob_sha]["sha"] = blob_sha.upper()
        result = await self.client.search_repository_content(
            "Microsoft/IssueLens", "needle", ref="Release/Topic"
        )
        self.assertEqual(result["resolved_ref"], OLD_COMMIT)
        self.assertEqual(result["items"][0]["name"], "space #?% \u00fc.txt")
        self.assertEqual(
            result["items"][0]["html_url"],
            f"https://github.com/Microsoft/IssueLens/blob/{OLD_COMMIT}/src/space%20%23%3F%25%20%C3%BC.txt",
        )
        self.assertIn(b"Release%2FTopic", self.requests[0].url.raw_path)
        self.assertEqual(self.provider.calls[0], ("Microsoft/IssueLens", {"contents": "read"}))

    async def test_truncated_tree_fails_before_blob_reads(self):
        self.add_file("found.txt", b"needle")
        self.trees[OLD_TREE]["truncated"] = True
        with self.assertRaisesRegex(GitHubAppError, "truncated"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_file_count_limit_fails_before_blob_reads(self):
        for index in range(65):
            self.add_file(f"{index:02}.txt", b"needle")
        with self.assertRaisesRegex(GitHubAppError, "64 regular files"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_total_content_limit_fails_before_blob_reads(self):
        for index in range(5):
            self.add_file(f"{index}.txt", b"x" * (64 * 1024))
        with self.assertRaisesRegex(GitHubAppError, "262144 content bytes"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_exact_file_and_byte_limits_are_accepted(self):
        clients = self.track_clients()
        for index in range(64):
            self.add_file(f"{index:02}.txt", b"needle" + b"x" * (4096 - 6))
        results = []
        for available in (True, False):
            with self.subTest(available=available):
                self.provider.available = available
                self.provider.calls.clear()
                self.requests.clear()
                result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=100)
                results.append(result)
                self.assertEqual(result["total_count"], 64)
                self.assertEqual(len(result["items"]), 64)
                self.assertEqual(len(self.requests), 66)
                self.assertEqual(len(clients), len(results))
                self.assertEqual(clients[-1].send.await_count, 66)
                self.assert_clients_closed(clients)
                self.assertEqual(self.provider.calls, [(REPOSITORY, {"contents": "read"})])
                expected_auth = "Bearer fake-repository-token" if available else None
                self.assertTrue(all(request.headers.get("Authorization") == expected_auth for request in self.requests))
                self.assertFalse(result["incomplete_results"])
                self.assertNotIn("fake-repository-token", json.dumps(result))
        self.assertEqual(*results)

    async def test_real_provider_auth_overhead_is_separate_from_66_content_requests(self):
        for index in range(64):
            self.add_file(f"{index:02}.txt", b"needle")
        results = []
        for installed in (True, False):
            with self.subTest(installed=installed):
                self.requests.clear()
                lookup = self.use_real_provider(installed=installed)
                result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=100)
                results.append(result)
                lookup.assert_awaited_once_with(REPOSITORY, {"contents": "read"})
                self.assertEqual(len(self.requests), 66)
                self.assertEqual(len(self.auth_requests), 2)
                self.assertEqual(len(self.requests) + len(self.auth_requests), 68)
                installation_path = f"/repos/{REPOSITORY}/installation"
                self.assertEqual([request.url.path for request in self.auth_requests], [
                    installation_path,
                    "/app/installations/1234/access_tokens" if installed else installation_path,
                ])
                expected_auth = "Bearer fake-repository-token" if installed else None
                self.assertTrue(all(request.headers.get("Authorization") == expected_auth for request in self.requests))
                self.requests.clear()
                self.auth_requests.clear()
                repeated = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=100)
                self.assertEqual(repeated, result)
                self.assertEqual(lookup.await_count, 2)
                self.assertEqual(len(self.requests), 66)
                self.assertEqual(len(self.auth_requests), 0 if installed else 2)
        self.assertEqual(*results)

    async def test_next_scan_retries_installation_after_anonymous_success(self):
        self.add_file("file.txt", b"needle")
        lookup = self.use_real_provider(installed=False)
        anonymous = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertTrue(all("Authorization" not in request.headers for request in self.requests))
        self.assertEqual(len(self.auth_requests), 2)

        self.installation_available = True
        self.requests.clear()
        authenticated = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(authenticated, anonymous)
        self.assertEqual(lookup.await_count, 2)
        self.assertEqual(len(self.auth_requests), 4)
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(request.headers.get("Authorization") == "Bearer fake-repository-token" for request in self.requests))

    async def test_failed_scan_does_not_retain_anonymous_auth(self):
        blob_sha = self.add_file("file.txt", b"needle")
        routes = (f"commits/{OLD_COMMIT}", f"git/trees/{OLD_TREE}", f"git/blobs/{blob_sha}")
        for completed, route in enumerate(routes, start=1):
            for failure in (httpx.Response(404), httpx.ReadError("secret-response-sentinel")):
                with self.subTest(route=route, failure=type(failure).__name__):
                    self.requests.clear()
                    lookup = self.use_real_provider(installed=False)
                    self.overrides[route] = failure
                    with self.assertRaises(GitHubAppError) as raised:
                        await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                    self.assertNotIn("secret-response-sentinel", str(raised.exception))
                    self.assertEqual(len(self.requests), completed)
                    self.assertEqual(len(self.auth_requests), 2)
                    lookup.assert_awaited_once_with(REPOSITORY, {"contents": "read"})
                    self.assertTrue(all("Authorization" not in request.headers for request in self.requests))

                    self.installation_available = True
                    self.overrides.clear()
                    self.requests.clear()
                    result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                    self.assertEqual(result["total_count"], 1)
                    self.assertEqual(lookup.await_count, 2)
                    self.assertEqual(len(self.auth_requests), 4)
                    self.assertEqual(len(self.requests), 3)
                    self.assertTrue(all(request.headers.get("Authorization") == "Bearer fake-repository-token" for request in self.requests))

    async def test_content_errors_keep_auth_mode_and_never_fall_back(self):
        clients = self.track_clients()
        blob_sha = self.add_file("file.txt", b"needle")
        routes = (f"commits/{OLD_COMMIT}", f"git/trees/{OLD_TREE}", f"git/blobs/{blob_sha}")
        for available in (True, False):
            self.provider.available = available
            for completed, route in enumerate(routes, start=1):
                for status, headers in ((401, {}), (403, {}), (403, {"x-ratelimit-remaining": "0"}), (404, {}), (429, {}), (500, {})):
                    with self.subTest(available=available, route=route, status=status, headers=headers):
                        self.requests.clear()
                        self.provider.calls.clear()
                        self.overrides = {route: httpx.Response(status, headers=headers, text="secret-response-sentinel")}
                        message = f"HTTP {status}" if available else (
                            "anonymous public-read rate limit exceeded" if headers else "not publicly readable"
                        )
                        with self.assertRaisesRegex(GitHubAppError, message) as raised:
                            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                        self.assertNotIn("secret-response-sentinel", str(raised.exception))
                        self.assertEqual(self.provider.calls, [(REPOSITORY, {"contents": "read"})])
                        self.assertEqual([request.url.path for request in self.requests], [
                            f"/repos/{REPOSITORY}/{expected}" for expected in routes[:completed]
                        ])
                        expected_auth = "Bearer fake-repository-token" if available else None
                        self.assertTrue(all(request.headers.get("Authorization") == expected_auth for request in self.requests))
                        self.assert_clients_closed(clients)

    async def test_concurrent_repository_scans_have_independent_auth(self):
        clients = self.track_clients()
        blob_sha = self.add_file("file.txt", b"needle")
        other_repository = "other/project"
        repositories = (REPOSITORY, other_repository)
        credential = await self.provider.get_token(REPOSITORY, {"contents": "read"})
        for other_installed in (True, False):
            with self.subTest(other_installed=other_installed):
                started = {repository: asyncio.Event() for repository in repositories}
                requests = []

                async def get_token(repository, permissions):
                    self.assertEqual(permissions, {"contents": "read"})
                    if repository == other_repository and not other_installed:
                        raise GitHubAppError("No App installation")
                    return replace(credential, repository=repository, token=f"fake-{repository}-token")

                async def handler(request):
                    requests.append(request)
                    self.assertEqual(request.method, "GET")
                    self.assertEqual(request.url.host, "api.github.com")
                    repository = "/".join(request.url.path.split("/")[2:4])
                    self.assertIn(repository, repositories)
                    expected_auth = None if repository == other_repository and not other_installed else f"Bearer fake-{repository}-token"
                    self.assertEqual(request.headers.get("Authorization"), expected_auth)
                    route = request.url.path.removeprefix(f"/repos/{repository}/")
                    if route == f"commits/{OLD_COMMIT}":
                        started[repository].set()
                        other = other_repository if repository == REPOSITORY else REPOSITORY
                        await asyncio.wait_for(started[other].wait(), timeout=2)
                        return httpx.Response(200, json=self.commits[OLD_COMMIT])
                    if route == f"git/trees/{OLD_TREE}":
                        return httpx.Response(200, json=self.trees[OLD_TREE])
                    self.assertEqual(route, f"git/blobs/{blob_sha}")
                    return httpx.Response(200, json=self.blobs[blob_sha])

                with patch.object(self.provider, "get_token", new=AsyncMock(side_effect=get_token)) as lookup:
                    client = GitHubClient(self.provider, transport=httpx.MockTransport(handler))
                    results = await asyncio.gather(*(
                        client.search_repository_content(repository, "needle", ref=OLD_COMMIT)
                        for repository in repositories
                    ))
                    self.assertEqual(lookup.await_count, 2)
                    lookup.assert_has_awaits([call(repository, {"contents": "read"}) for repository in repositories], any_order=True)
                self.assertEqual(len(requests), 6)
                self.assertEqual(len(clients), 2 if other_installed else 4)
                self.assertIsNot(clients[-1], clients[-2])
                self.assert_clients_closed(clients)
                client_repositories = []
                for http_client in clients[-2:]:
                    self.assertEqual(http_client.send.await_count, 3)
                    scopes = {
                        "/".join(sent.kwargs["request"].url.path.split("/")[2:4])
                        for sent in http_client.send.await_args_list
                    }
                    self.assertEqual(len(scopes), 1)
                    client_repositories.extend(scopes)
                self.assertCountEqual(client_repositories, repositories)
                for repository, result in zip(repositories, results):
                    self.assertEqual(result["total_count"], 1)
                    self.assertEqual(result["items"][0]["repository"], {"full_name": repository})

    async def test_scoped_auth_is_immutable_and_rejects_other_request_scopes(self):
        credential = await self.provider.get_token(REPOSITORY, {"contents": "read"})
        self.provider.calls.clear()
        client = GitHubClient(self.provider, writes_enabled=True, transport=httpx.MockTransport(self.handler))
        for resolved in (credential, None):
            context = _ContentReadAuth(REPOSITORY, resolved)
            self.assertNotIn("fake-repository-token", repr(context))
            with self.assertRaises(FrozenInstanceError):
                context.repository = "other/project"
            for override in (
                {"repository": "other/project"}, {"method": "POST"}, {"method": "HEAD"},
                {"write": True}, {"permissions": {"contents": "write"}},
                {"permissions": {"issues": "read"}}, {"permissions": {"contents": "read", "metadata": "read"}},
                {"absolute_url": "https://api.github.com/repos/other/project/git/trees/main"},
            ):
                with self.subTest(anonymous=resolved is None, override=override):
                    arguments = {"method": "GET", "repository": REPOSITORY, "permissions": {"contents": "read"}, **override}
                    with self.assertRaisesRegex(GitHubAppError, "scope mismatch"):
                        await client._request(**arguments, content_read_auth=context)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.requests, [])

    async def test_scoped_auth_rejects_wrong_credential_scope(self):
        credential = await self.provider.get_token(REPOSITORY, {"contents": "read"})
        self.provider.calls.clear()
        for invalid in (
            replace(credential, repository="other/project"),
            replace(credential, permissions=(("contents", "write"),)),
        ):
            with self.subTest(permissions=invalid.permissions, repository=invalid.repository):
                with self.assertRaisesRegex(GitHubAppError, "scope mismatch"):
                    await self.client._request(
                        "GET", REPOSITORY, permissions={"contents": "read"},
                        content_read_auth=_ContentReadAuth(REPOSITORY, invalid),
                    )
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.requests, [])

    async def test_expiring_scan_credential_fails_closed_without_refresh(self):
        self.add_file("file.txt", b"needle")
        credential = await self.provider.get_token(REPOSITORY, {"contents": "read"})
        for completed in range(3):
            with self.subTest(completed=completed):
                self.requests.clear()
                with patch.object(self.provider, "get_token", new=AsyncMock(return_value=replace(credential, expires_at=100))) as lookup:
                    with patch("github_app_mcp.src.issuelens_github_mcp.github.time") as clock:
                        clock.time.side_effect = [99] * completed + [100]
                        with self.assertRaisesRegex(GitHubAppError, "scan credential expired; retry the search"):
                            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                    lookup.assert_awaited_once_with(REPOSITORY, {"contents": "read"})
                    self.assertEqual(len(self.requests), completed)
                    self.assertTrue(all(request.headers.get("Authorization") == "Bearer fake-repository-token" for request in self.requests))
                    lookup.return_value = credential
                    self.requests.clear()
                    result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                    self.assertEqual(result["total_count"], 1)
                    self.assertEqual(lookup.await_count, 2)
                    self.assertEqual(len(self.requests), 3)

    async def test_skipped_files_are_explicit_and_never_follow_links(self):
        regular_blob = self.add_file("regular.txt", b"needle")
        self.add_file("binary.bin", b"needle\x00hidden")
        self.add_file("legacy.txt", b"needle\xff")
        unsupported = self.add_file("encoding.txt", b"needle unsupported")
        self.blobs[unsupported]["encoding"] = "rot13"
        symlink_blob = self.add_file("symlink", b"../../needle", mode="120000")
        oversized = self.add_file("huge.txt", b"needle" + b"x" * (64 * 1024))
        self.trees[OLD_TREE]["tree"].extend([
            {"path": "vendor", "type": "commit", "mode": "160000", "sha": NEW_COMMIT},
            {"path": "directory", "type": "tree", "mode": "040000", "sha": NEW_TREE},
        ])

        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual([item["sha"] for item in result["items"]], [regular_blob])
        self.assertEqual(result["total_count"], 1)
        self.assertTrue(result["incomplete_results"])
        self.assertEqual(result["skipped_files"], 6)
        self.assertEqual(result["skipped_reasons"], {
            "binary": 1, "non_utf8": 1, "unsupported_encoding": 1,
            "file_too_large": 1, "symlink": 1, "submodule": 1,
        })
        paths = [request.url.path for request in self.requests]
        self.assertNotIn(f"/repos/{REPOSITORY}/git/blobs/{symlink_blob}", paths)
        self.assertNotIn(f"/repos/{REPOSITORY}/git/blobs/{oversized}", paths)
        no_match = await self.client.search_repository_content(REPOSITORY, "absent", ref=OLD_COMMIT)
        self.assertEqual(no_match["items"], [])
        self.assertTrue(no_match["incomplete_results"])
        self.assertEqual(no_match["skipped_files"], 6)

    async def test_empty_snapshot_is_complete(self):
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total_count"], 0)
        self.assertEqual(result["skipped_reasons"], {})
        self.assertFalse(result["incomplete_results"])
        self.assertEqual(len(self.requests), 2)

    async def test_huge_matches_have_bounded_excerpts_and_line_count(self):
        self.add_file("long.txt", b"x" * (64 * 1024 - 6) + b"needle")
        self.add_file("lines.txt", b"needle\n" * 1000)
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(result["items"]), 2)
        line_matches, long_match = (item["matches"] for item in result["items"])
        self.assertEqual([match["line_number"] for match in line_matches], [1, 2, 3])
        self.assertEqual(len(long_match[0]["excerpt"]), 160)
        self.assertTrue(long_match[0]["truncated"])
        self.assertEqual(long_match[0]["line_number"], 1)
        self.assertLess(len(json.dumps(result).encode("utf-8")), 2000)

    async def test_aggregate_result_limit_and_smaller_page(self):
        content = (("needle" + "\u4e00" * 154 + "\n") * 3).encode("utf-8")
        for index in range(64):
            self.add_file(f"{index:02}.txt", content)
        with self.assertRaisesRegex(GitHubAppError, "reduce per_page"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=100)
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["total_count"], 64)
        self.assertFalse(result["incomplete_results"])

    async def test_line_numbers_count_source_newlines_not_unicode_separators(self):
        self.add_file("lines.txt", "prefix\f\u0085\u2028\r\nneedle\r\n".encode("utf-8"))
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(result["items"][0]["matches"], [
            {"line_number": 2, "excerpt": "needle", "truncated": False},
        ])

    async def test_oversized_tree_honors_request_result_limit(self):
        self.trees[OLD_TREE]["padding"] = "x" * 100_000
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_malformed_commit_payloads_fail_safely(self):
        valid = copy.deepcopy(self.commits[OLD_COMMIT])
        for payload in (
            [], {}, {"sha": OLD_COMMIT, "commit": []},
            {"sha": OLD_COMMIT, "commit": {"tree": None}},
            {**valid, "sha": "short"}, {**valid, "sha": NEW_COMMIT},
            {**valid, "sha": [OLD_COMMIT]},
            {**valid, "commit": {"tree": {"sha": "../secret-response-sentinel"}}},
        ):
            with self.subTest(payload=payload):
                self.requests.clear()
                self.commits[OLD_COMMIT] = payload
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 1)

    async def test_malformed_tree_payloads_fail_before_blob_reads(self):
        self.add_file("file.txt", b"needle")
        valid = copy.deepcopy(self.trees[OLD_TREE])
        entry = valid["tree"][0]
        bad_entries = (
            None, {**entry, "path": "../secret-response-sentinel"},
            {**entry, "path": "a/./file.txt"}, {**entry, "path": "file\n.txt"},
            {**entry, "size": True}, {**entry, "size": -1}, {**entry, "size": "6"},
            {**entry, "mode": "100600"}, {**entry, "sha": "short"},
        )
        payloads = [
            [], {}, {**valid, "sha": NEW_TREE}, {**valid, "truncated": 1},
            {**valid, "tree": {}}, {**valid, "tree": [entry, entry]},
            {"sha": OLD_TREE, "tree": []},
            *({**valid, "tree": [bad]} for bad in bad_entries),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.requests.clear()
                self.trees[OLD_TREE] = payload
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 2)

    async def test_malformed_blob_payloads_fail_safely(self):
        blob_sha = self.add_file("file.txt", b"needle")
        valid = copy.deepcopy(self.blobs[blob_sha])
        for payload in (
            [], {}, {**valid, "sha": NEW_COMMIT}, {**valid, "size": True},
            {**valid, "size": -1}, {**valid, "size": 65537},
            {**valid, "content": []}, {**valid, "encoding": None},
            {**valid, "content": "%%%%%%%%"},
            {**valid, "content": base64.b64encode(b"short").decode("ascii")},
            {**valid, "content": "secret-response-sentinel" * 10},
        ):
            with self.subTest(payload=payload):
                self.requests.clear()
                self.blobs[blob_sha] = payload
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 3)

    async def test_blob_read_failure_and_invalid_json_do_not_fall_back(self):
        blob_sha = self.add_file("file.txt", b"needle")
        for response in (
            httpx.Response(404),
            httpx.Response(200, content=b"secret-response-sentinel is not JSON"),
            httpx.Response(200, content=b"x" * (128 * 1024 + 1)),
        ):
            with self.subTest(response=response):
                self.requests.clear()
                self.overrides[f"git/blobs/{blob_sha}"] = response
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 3)
                self.assertTrue(all(request.url.path != "/search/code" for request in self.requests))
