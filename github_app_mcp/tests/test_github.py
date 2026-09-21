import base64
import json
import os
import pathlib
import sys
import unittest

import httpx


PACKAGE_ROOT = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp.auth import (  # noqa: E402
    GitHubAppError,
    InstallationCredential,
)
from issuelens_github_mcp.github import GitHubClient  # noqa: E402


class RecordingProvider:
    def __init__(self):
        self.calls = []

    async def get_token(self, repository, permissions):
        self.calls.append((repository, permissions))
        return InstallationCredential(
            installation_id=1234,
            repository=repository,
            permissions=tuple(sorted(permissions.items())),
            token="repository-token",
            expires_at=float("inf"),
        )


class FailingProvider:
    def __init__(self):
        self.calls = []

    async def get_token(self, repository, permissions):
        self.calls.append((repository, permissions))
        raise GitHubAppError("App installation unavailable")


class GitHubClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.provider = RecordingProvider()

        def handler(request):
            self.requests.append(request)
            if request.url.path == "/repos/microsoft/IssueLens/issues":
                return httpx.Response(200, json=[
                    {"number": 1, "title": "Issue"},
                    {"number": 2, "pull_request": {}, "title": "PR"},
                ])
            if request.url.path == "/search/issues":
                return httpx.Response(200, json={"items": [{"number": 1}]})
            if request.url.path == "/repos/microsoft/IssueLens/contents/README.md":
                return httpx.Response(200, json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(b"hello").decode("ascii"),
                })
            if request.url.path.endswith("/labels"):
                return httpx.Response(200, json=[{"name": "bug"}])
            if request.url.path.endswith("/comments"):
                return httpx.Response(201, json={"id": 42})
            if request.url.path == "/repos/microsoft/IssueLens/issues/comments/99":
                return httpx.Response(200, json={
                    "id": 99,
                    "body": "@issuelens replan",
                    "author_association": "MEMBER",
                    "user": {"login": "maintainer", "type": "User"},
                    "issue_url": (
                        "https://api.github.com/repos/microsoft/IssueLens/issues/1"
                    ),
                    "html_url": (
                        "https://github.com/microsoft/IssueLens/issues/1#issuecomment-99"
                    ),
                    "created_at": "2026-08-17T00:00:00Z",
                    "updated_at": "2026-08-17T00:00:00Z",
                    "ignored_field": "not exposed",
                })
            if request.url.path == "/repos/microsoft/IssueLens/issues/1":
                return httpx.Response(200, json={
                    "number": 1,
                    "body": (
                        "![screenshot](https://github.com/user-attachments/"
                        "assets/12345678-1234-1234-1234-123456789abc)\n"
                        "![ignored](https://example.com/internal.png)"
                    ),
                })
            if request.url.path == (
                "/user-attachments/assets/"
                "12345678-1234-1234-1234-123456789abc"
            ):
                return httpx.Response(302, headers={
                    "Location": (
                        "https://github-production-user-asset-1.s3.amazonaws.com/"
                        "123/image.png?signature=test"
                    )
                })
            if request.url.host == (
                "github-production-user-asset-1.s3.amazonaws.com"
            ):
                return httpx.Response(
                    200,
                    content=b"\x89PNG\r\n\x1a\nimage bytes",
                    headers={"Content-Type": "image/png"},
                )
            return httpx.Response(200, json={"full_name": "microsoft/IssueLens"})

        self.transport = httpx.MockTransport(handler)

    def client(self, *, writes_enabled=False):
        return GitHubClient(
            self.provider,
            writes_enabled=writes_enabled,
            transport=self.transport,
        )

    async def test_repository_syntax_is_checked_before_token_minting(self):
        with self.assertRaisesRegex(GitHubAppError, "owner/repository"):
            await self.client().get_repository("not-a-repository")

        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.requests, [])

    async def test_compare_encodes_each_ref_as_a_path_component(self):
        for base, head in (
            ("release/1.2", "feature/topic"),
            ("release/1.2", "main"),
            ("refs/tags/v1.0", "a" * 40),
        ):
            with self.subTest(base=base, head=head):
                await self.client().compare_commits("microsoft/IssueLens", base, head)
                encoded_base = base.replace("/", "%2F")
                encoded_head = head.replace("/", "%2F")
                self.assertEqual(
                    self.requests[-1].url.raw_path,
                    f"/repos/microsoft/IssueLens/compare/{encoded_base}...{encoded_head}".encode(),
                )
                self.assertEqual(self.provider.calls[-1], ("microsoft/IssueLens", {"contents": "read"}))

    def commit_client(self, patch="+new\n", *, file_count=1):
        payload = {
            "sha": "a" * 40,
            "commit": {"message": "Change source", "tree": {"sha": "b" * 40}},
            "parents": [{"sha": "c" * 40}],
            "stats": {"additions": file_count, "deletions": 0, "total": file_count},
            "files": [{
                "sha": "d" * 40, "filename": f"src/file-{index}.py",
                "status": "modified", "additions": 1, "deletions": 0, "changes": 1,
                "patch": patch,
            } for index in range(file_count)],
        }

        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json=payload)

        return GitHubClient(self.provider, transport=httpx.MockTransport(handler)), payload

    async def test_commit_defaults_to_stats_without_losing_source_identity(self):
        client, payload = self.commit_client()
        result = await client.get_commit("microsoft/IssueLens", payload["sha"])
        self.assertEqual(result["sha"], payload["sha"])
        self.assertEqual(result["parents"], payload["parents"])
        self.assertEqual(result["commit"]["tree"], payload["commit"]["tree"])
        self.assertEqual(result["stats"], payload["stats"])
        self.assertEqual(result["files"], [{
            key: value for key, value in payload["files"][0].items() if key != "patch"
        }])
        self.assertEqual(dict(self.requests[0].url.params), {"per_page": "30", "page": "1"})
        self.assertEqual(self.provider.calls, [("microsoft/IssueLens", {"contents": "read"})])

    async def test_commit_none_and_full_patch_keep_the_requested_detail(self):
        client, payload = self.commit_client()
        for detail in ("none", "stats", "full_patch"):
            with self.subTest(detail=detail):
                result = await client.get_commit(
                    "microsoft/IssueLens", payload["sha"], detail=detail, per_page=1, page=2,
                )
                self.assertEqual(dict(self.requests[-1].url.params), {"per_page": "1", "page": "2"})
                self.assertEqual(result["parents"], payload["parents"])
                if detail == "none":
                    self.assertNotIn("files", result)
                    self.assertNotIn("stats", result)
                elif detail == "stats":
                    self.assertNotIn("patch", result["files"][0])
                else:
                    self.assertEqual(result, payload)
        self.assertEqual(len(self.requests), 3)

    async def test_commit_projection_handles_large_transport_without_enlarging_model_results(self):
        client, payload = self.commit_client("+source\n" * 10000, file_count=5)
        self.assertGreater(len(json.dumps(payload).encode()), 128 * 1024)
        for detail in ("none", "stats"):
            with self.subTest(detail=detail):
                result = await client.get_commit("microsoft/IssueLens", payload["sha"], detail=detail)
                self.assertLess(len(json.dumps(result, ensure_ascii=True).encode()), 100_000)
                self.assertNotIn("+source", json.dumps(result))
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await client.get_commit("microsoft/IssueLens", payload["sha"], detail="full_patch")
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await client.get_repository("microsoft/IssueLens")

    async def test_commit_download_stays_bounded_even_when_files_are_omitted(self):
        client, payload = self.commit_client("x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await client.get_commit("microsoft/IssueLens", payload["sha"], detail="none")

    async def test_commit_patch_budget_counts_json_escapes(self):
        client, payload = self.commit_client("\u00e9" * 20000)
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await client.get_commit("microsoft/IssueLens", payload["sha"], detail="full_patch")
        stats = await client.get_commit("microsoft/IssueLens", payload["sha"])
        self.assertNotIn("patch", stats["files"][0])

    async def test_commit_controls_are_validated_before_authentication(self):
        cases = [
            {"detail": "diff"}, {"detail": None}, {"detail": []},
            {"per_page": 0}, {"per_page": 101}, {"per_page": True},
            {"page": 0}, {"page": 3001}, {"page": True},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(GitHubAppError):
                    await self.client().get_commit("microsoft/IssueLens", "a" * 40, **arguments)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.requests, [])

    async def test_file_pagination_supports_small_pages_beyond_the_old_page_limit(self):
        client, payload = self.commit_client()
        await client.get_commit("microsoft/IssueLens", payload["sha"], per_page=1, page=3000)
        await client.list_pull_request_files("microsoft/IssueLens", 34, per_page=1, page=3000)
        self.assertTrue(all(request.url.params["page"] == "3000" for request in self.requests))
        with self.assertRaisesRegex(GitHubAppError, "page"):
            await client.list_pull_request_files("microsoft/IssueLens", 34, per_page=1, page=3001)
        with self.assertRaisesRegex(GitHubAppError, "page"):
            await client.list_issues("microsoft/IssueLens", page=101)
        self.assertEqual(len(self.requests), 2)

    async def test_large_pr_patches_are_readable_through_existing_single_file_pages(self):
        files = [{
            "filename": f"src/file-{index}.py", "status": "modified",
            "patch": "+source line\n" * (6000 if index == 0 else 1000),
        } for index in range(23)]

        def handler(request):
            self.requests.append(request)
            self.assertEqual(request.url.path, "/repos/microsoft/IssueLens/pulls/34/files")
            per_page, page = int(request.url.params["per_page"]), int(request.url.params["page"])
            start = (page - 1) * per_page
            return httpx.Response(200, json=files[start:start + per_page])

        client = GitHubClient(self.provider, transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await client.list_pull_request_files("microsoft/IssueLens", 34)
        collected = []
        for page in range(1, len(files) + 2):
            result = await client.list_pull_request_files("microsoft/IssueLens", 34, per_page=1, page=page)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode()), 100_000)
            collected.extend(result)
        self.assertEqual(result, [])
        self.assertEqual(collected, files)
        self.assertTrue(all(permissions == {"pull_requests": "read"} for _, permissions in self.provider.calls))

    async def test_missing_or_oversized_individual_patches_are_not_fabricated_or_truncated(self):
        client, payload = self.commit_client()
        payload["files"][0].pop("patch")
        result = await client.get_commit("microsoft/IssueLens", payload["sha"], detail="full_patch")
        self.assertNotIn("patch", result["files"][0])
        payload["files"][0]["patch"] = "x" * 100_000
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await client.get_commit(
                "microsoft/IssueLens", payload["sha"], detail="full_patch", per_page=1,
            )

    async def test_tree_ref_is_one_encoded_path_component(self):
        for recursive in (False, True):
            with self.subTest(recursive=recursive):
                await self.client().list_repository_tree(
                    "microsoft/IssueLens", "release/1.2", recursive=recursive,
                )
                self.assertEqual(
                    self.requests[-1].url.raw_path.split(b"?", 1)[0],
                    b"/repos/microsoft/IssueLens/git/trees/release%2F1.2",
                )
                self.assertEqual(dict(self.requests[-1].url.params), {"recursive": "1"} if recursive else {})
                self.assertEqual(self.provider.calls[-1], ("microsoft/IssueLens", {"contents": "read"}))

    async def test_write_gate_is_checked_before_token_minting(self):
        with self.assertRaisesRegex(GitHubAppError, "write tools are disabled"):
            await self.client().add_labels(
                "microsoft/IssueLens", 1, ["bug"]
            )

        self.assertEqual(self.provider.calls, [])

    async def test_refs_cannot_inject_search_syntax_or_git_expressions(self):
        invalid_refs = (
            "main\nOR\nrepo:other", "main\tOR\trepo:other", "main\r", "main\x00",
            "main\x1f", "main\x7f", "main\u0085", "main\u200b", "main\u2028",
            "main:other", "HEAD~1", "HEAD^", "main*", "main[0]", "main?",
            "main\\other", "main..other", "main@{1}", "@", "-main", "/main",
            "main/", "main//topic", ".hidden/topic", "main/.hidden", "main.lock",
            "branch.lock/topic", "main.", 'main"', "main'", "main%0aOR",
        )
        client = self.client()
        for ref in invalid_refs:
            with self.subTest(ref=repr(ref)):
                with self.assertRaises(GitHubAppError):
                    await client.list_merged_pull_requests("microsoft/IssueLens", base=ref)
                with self.assertRaises(GitHubAppError):
                    await client.list_repository_tree("microsoft/IssueLens", ref)
                with self.assertRaises(GitHubAppError):
                    await client.compare_commits("microsoft/IssueLens", ref, "main")
                with self.assertRaises(GitHubAppError):
                    await client.get_file("microsoft/IssueLens", "README.md", ref=ref)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.requests, [])

    async def test_valid_branch_names_remain_scoped_in_merged_pr_search(self):
        for ref in ("main", "release/1.2", "refs/heads/feature/topic", "a" * 40):
            with self.subTest(ref=ref):
                await self.client().list_merged_pull_requests("microsoft/IssueLens", base=ref)
                self.assertEqual(
                    self.requests[-1].url.params["q"],
                    f"repo:microsoft/IssueLens is:pr is:merged base:{ref}",
                )

    async def test_reaction_write_gate_is_checked_before_token_minting(self):
        with self.assertRaisesRegex(GitHubAppError, "write tools are disabled"):
            await self.client().add_eyes_reaction(
                "microsoft/IssueLens",
                "issue",
                1,
            )

        self.assertEqual(self.provider.calls, [])

    async def test_list_issues_filters_pull_requests_and_uses_read_permission(self):
        issues = await self.client().list_issues(
            "microsoft/IssueLens", per_page=50, page=2
        )

        self.assertEqual([issue["number"] for issue in issues], [1])
        self.assertEqual(
            self.provider.calls[-1],
            ("microsoft/IssueLens", {"issues": "read"}),
        )
        self.assertEqual(self.requests[-1].url.params["page"], "2")

    async def test_get_issue_comment_is_bounded_to_requested_issue(self):
        result = await self.client().get_issue_comment(
            "microsoft/IssueLens", 1, 99
        )

        self.assertEqual(result, {
            "id": 99,
            "body": "@issuelens replan",
            "author_association": "MEMBER",
            "user": {"login": "maintainer", "type": "User"},
            "created_at": "2026-08-17T00:00:00Z",
            "updated_at": "2026-08-17T00:00:00Z",
            "html_url": (
                "https://github.com/microsoft/IssueLens/issues/1#issuecomment-99"
            ),
        })
        self.assertEqual(
            self.requests[-1].url.path,
            "/repos/microsoft/IssueLens/issues/comments/99",
        )
        self.assertEqual(
            self.provider.calls[-1],
            ("microsoft/IssueLens", {"issues": "read"}),
        )

    async def test_get_issue_comment_rejects_cross_issue_comment(self):
        with self.assertRaisesRegex(GitHubAppError, "requested issue"):
            await self.client().get_issue_comment(
                "microsoft/IssueLens", 2, 99
            )

    async def test_get_issue_comment_accepts_canonical_repository_casing(self):
        payload = {
            "id": 99,
            "body": "@issuelens replan",
            "author_association": "MEMBER",
            "user": {"login": "maintainer", "type": "User"},
            "issue_url": (
                "https://api.github.com/repos/microsoft/IssueLens/issues/1"
            ),
        }
        client = GitHubClient(
            self.provider,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            ),
        )

        result = await client.get_issue_comment(
            "microsoft/issuelens",
            1,
            99,
        )

        self.assertEqual(result["id"], 99)
        self.assertEqual(
            self.provider.calls[-1],
            ("microsoft/issuelens", {"issues": "read"}),
        )

    async def test_get_issue_comment_validates_ids_before_token_minting(self):
        for issue_number, comment_id in ((0, 99), (1, 0)):
            with self.subTest(
                issue_number=issue_number,
                comment_id=comment_id,
            ):
                with self.assertRaisesRegex(GitHubAppError, "positive integer"):
                    await self.client().get_issue_comment(
                        "microsoft/IssueLens",
                        issue_number,
                        comment_id,
                    )

        self.assertEqual(self.provider.calls, [])

    async def test_get_issue_comment_rejects_invalid_authoritative_shape(self):
        issue_url = "https://api.github.com/repos/microsoft/IssueLens/issues/1"
        invalid_payloads = (
            {
                "id": 100,
                "body": "@issuelens plan",
                "author_association": "MEMBER",
                "user": {"login": "maintainer", "type": "User"},
                "issue_url": issue_url,
            },
            {
                "id": 99,
                "body": None,
                "author_association": "MEMBER",
                "user": {"login": "maintainer", "type": "User"},
                "issue_url": issue_url,
            },
            {
                "id": 99,
                "body": "@issuelens plan",
                "author_association": None,
                "user": {"login": "maintainer", "type": "User"},
                "issue_url": issue_url,
            },
            {
                "id": 99,
                "body": "@issuelens plan",
                "author_association": "MEMBER",
                "user": {"login": "", "type": "User"},
                "issue_url": issue_url,
            },
            {
                "id": 99,
                "body": "@issuelens plan",
                "author_association": "MEMBER",
                "user": {"login": "maintainer", "type": None},
                "issue_url": issue_url,
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                client = GitHubClient(
                    self.provider,
                    transport=httpx.MockTransport(
                        lambda request, response=payload: httpx.Response(
                            200,
                            json=response,
                        )
                    ),
                )
                with self.assertRaisesRegex(
                    GitHubAppError,
                    "invalid issue comment",
                ):
                    await client.get_issue_comment(
                        "microsoft/IssueLens",
                        1,
                        99,
                    )

    async def test_search_rejects_scope_override_before_token_minting(self):
        for query in (
            "repo:other/repo crash",
            "+repo:other/repo crash",
            "-repo:other/repo crash",
            "crash,(org:other)",
        ):
            with self.subTest(query=query):
                with self.assertRaisesRegex(GitHubAppError, "qualifiers"):
                    await self.client().search_issues(
                        "microsoft/IssueLens", query
                    )

        self.assertEqual(self.provider.calls, [])

    async def test_search_adds_fixed_repository_and_issue_scope(self):
        result = await self.client().search_issues(
            "microsoft/IssueLens", "startup crash"
        )

        self.assertEqual(result, [{"number": 1}])
        self.assertEqual(
            self.requests[-1].url.params["q"],
            "repo:microsoft/IssueLens is:issue startup crash",
        )

    async def test_read_falls_back_to_anonymous_for_public_repository(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"full_name": "public/repo"})

        client = GitHubClient(
            FailingProvider(),
            transport=httpx.MockTransport(handler),
        )

        result = await client.get_repository("public/repo")

        self.assertEqual(result["full_name"], "public/repo")
        self.assertNotIn("Authorization", requests[0].headers)

    async def test_anonymous_fallback_reports_inaccessible_repository(self):
        client = GitHubClient(
            FailingProvider(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(404, json={"message": "Not Found"})
            ),
        )

        with self.assertRaisesRegex(GitHubAppError, "not publicly readable"):
            await client.get_repository("private/repo")

    async def test_anonymous_fallback_reports_rate_limit(self):
        client = GitHubClient(
            FailingProvider(),
            transport=httpx.MockTransport(lambda request: httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0"},
                json={"message": "rate limit"},
            )),
        )

        with self.assertRaisesRegex(GitHubAppError, "rate limit exceeded"):
            await client.search_issues("public/repo", "startup failure")

    async def test_get_file_decodes_bounded_utf8_content(self):
        result = await self.client().get_file(
            "microsoft/IssueLens", "README.md"
        )

        self.assertEqual(result["decoded_content"], "hello")
        self.assertNotIn("content", result)
        self.assertEqual(
            self.provider.calls[-1][1], {"contents": "read"}
        )

    async def test_get_file_rejects_traversal_before_token_minting(self):
        for path in ("..\\secret.pem", "docs//secret.md"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(GitHubAppError, "relative POSIX path"):
                    await self.client().get_file(
                        "microsoft/IssueLens", path
                    )

        self.assertEqual(self.provider.calls, [])

    async def test_get_file_encodes_url_significant_path_characters(self):
        await self.client().get_file(
            "microsoft/IssueLens", "docs/error?#%.md"
        )

        request = self.requests[-1]
        self.assertEqual(request.url.query, b"")
        self.assertIn(b"error%3F%23%25.md", request.url.raw_path)

    async def test_get_file_rejects_invalid_base64(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            json={"type": "file", "encoding": "base64", "content": "@@@"},
        ))
        client = GitHubClient(
            self.provider,
            transport=transport,
        )

        with self.assertRaisesRegex(GitHubAppError, "invalid base64"):
            await client.get_file("microsoft/IssueLens", "README.md")

    async def test_oversized_http_response_is_rejected_before_json_parsing(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            content=b'{' + b'"value":"' + (b"x" * (129 * 1024)) + b'"}',
        ))
        client = GitHubClient(
            self.provider,
            transport=transport,
        )

        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await client.get_repository("microsoft/IssueLens")

    async def test_write_tools_use_fixed_routes_and_write_permission(self):
        client = self.client(writes_enabled=True)

        await client.add_labels("microsoft/IssueLens", 1, ["bug"])
        label_request = self.requests[-1]
        await client.set_assignees("microsoft/IssueLens", 1, ["octocat"])
        assignee_request = self.requests[-1]
        await client.add_issue_comment("microsoft/IssueLens", 1, "Triage report")
        comment_request = self.requests[-1]

        self.assertEqual(label_request.method, "POST")
        self.assertEqual(
            label_request.url.path,
            "/repos/microsoft/IssueLens/issues/1/labels",
        )
        self.assertEqual(json.loads(label_request.content), {"labels": ["bug"]})
        self.assertEqual(assignee_request.method, "PATCH")
        self.assertEqual(
            json.loads(assignee_request.content), {"assignees": ["octocat"]}
        )
        self.assertEqual(
            comment_request.url.path,
            "/repos/microsoft/IssueLens/issues/1/comments",
        )
        self.assertTrue(
            all(call[1] == {"issues": "write"} for call in self.provider.calls)
        )

    async def test_eyes_reaction_uses_fixed_routes_payload_and_permissions(self):
        requests = []
        statuses = iter((200, 201, 200, 201))

        def handler(request):
            requests.append(request)
            return httpx.Response(next(statuses), json={"id": len(requests)})

        client = GitHubClient(
            self.provider,
            writes_enabled=True,
            transport=httpx.MockTransport(handler),
        )
        cases = (
            (
                "issue",
                1,
                "/repos/microsoft/IssueLens/issues/1/reactions",
                {"issues": "write"},
            ),
            (
                "pull_request",
                2,
                "/repos/microsoft/IssueLens/issues/2/reactions",
                {"issues": "write"},
            ),
            (
                "issue_comment",
                3,
                "/repos/microsoft/IssueLens/issues/comments/3/reactions",
                {"issues": "write"},
            ),
            (
                "pull_request_review_comment",
                4,
                "/repos/microsoft/IssueLens/pulls/comments/4/reactions",
                {"pull_requests": "write"},
            ),
        )

        results = []
        for target_kind, target_id, path, permission in cases:
            results.append(await client.add_eyes_reaction(
                "microsoft/IssueLens",
                target_kind,
                target_id,
            ))
            request = requests[-1]
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, path)
            self.assertEqual(
                json.loads(request.content),
                {"content": "eyes"},
            )
            self.assertEqual(
                self.provider.calls[-1],
                ("microsoft/IssueLens", permission),
            )

        self.assertEqual([result["id"] for result in results], [1, 2, 3, 4])
        self.assertEqual(len(requests), 4)

    async def test_eyes_reaction_retry_uses_identical_request(self):
        requests = []
        statuses = iter((201, 200))

        def handler(request):
            requests.append(request)
            return httpx.Response(next(statuses), json={"id": len(requests)})

        client = GitHubClient(
            self.provider,
            writes_enabled=True,
            transport=httpx.MockTransport(handler),
        )

        first = await client.add_eyes_reaction(
            "microsoft/IssueLens",
            "issue_comment",
            3,
        )
        second = await client.add_eyes_reaction(
            "microsoft/IssueLens",
            "issue_comment",
            3,
        )

        self.assertEqual(first["id"], 1)
        self.assertEqual(second["id"], 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].method, requests[1].method)
        self.assertEqual(requests[0].url, requests[1].url)
        self.assertEqual(requests[0].content, requests[1].content)
        self.assertEqual(json.loads(requests[1].content), {"content": "eyes"})

    async def test_eyes_reaction_rejects_invalid_target_before_token_minting(self):
        client = self.client(writes_enabled=True)

        with self.assertRaisesRegex(GitHubAppError, "target_kind"):
            await client.add_eyes_reaction(
                "microsoft/IssueLens",
                "discussion",
                1,
            )
        with self.assertRaisesRegex(GitHubAppError, "positive integer"):
            await client.add_eyes_reaction(
                "microsoft/IssueLens",
                "issue",
                0,
            )

        self.assertEqual(self.provider.calls, [])

    async def test_write_never_falls_back_to_anonymous(self):
        requests = []
        client = GitHubClient(
            FailingProvider(),
            writes_enabled=True,
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(201)
            ),
        )

        with self.assertRaisesRegex(GitHubAppError, "installation unavailable"):
            await client.add_labels("public/repo", 1, ["bug"])

        self.assertEqual(requests, [])

    async def test_multiple_comments_are_allowed_in_one_session(self):
        client = self.client(writes_enabled=True)

        await client.add_issue_comment(
            "microsoft/IssueLens",
            1,
            "First requested comment",
        )
        await client.add_issue_comment(
            "microsoft/IssueLens",
            1,
            "Second requested comment",
        )

        comment_requests = [
            request
            for request in self.requests
            if request.url.path.endswith("/issues/1/comments")
        ]
        self.assertEqual(len(comment_requests), 2)
        self.assertEqual(
            json.loads(comment_requests[0].content),
            {"body": "First requested comment"},
        )
        self.assertEqual(
            json.loads(comment_requests[1].content),
            {"body": "Second requested comment"},
        )

    async def test_issue_images_are_allowlisted_without_redirect_token_leak(self):
        result = await self.client().get_issue_images(
            "microsoft/IssueLens", 1
        )

        self.assertEqual(result["discovered_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["mime_type"], "image/png")
        self.assertEqual(
            base64.b64decode(result["images"][0]["data"]),
            b"\x89PNG\r\n\x1a\nimage bytes",
        )
        asset_call = next(
            request
            for request in self.requests
            if request.url.host == "github.com"
            and request.url.path.startswith("/user-attachments/")
        )
        redirect_call = next(
            request
            for request in self.requests
            if request.url.host == (
                "github-production-user-asset-1.s3.amazonaws.com"
            )
        )
        self.assertEqual(
            asset_call.headers["Authorization"], "Bearer repository-token"
        )
        self.assertNotIn("Authorization", redirect_call.headers)

    async def test_issue_image_rejects_content_type_spoofing(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            content=b"not really a png",
            headers={"Content-Type": "image/png"},
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaisesRegex(GitHubAppError, "does not match"):
                await GitHubClient._download_issue_image(
                    client,
                    "https://github.com/user-attachments/assets/"
                    "12345678-1234-1234-1234-123456789abc",
                    "repository-token",
                    1024,
                )


if __name__ == "__main__":
    unittest.main()
