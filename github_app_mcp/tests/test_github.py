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

    async def test_write_gate_is_checked_before_token_minting(self):
        with self.assertRaisesRegex(GitHubAppError, "write tools are disabled"):
            await self.client().add_labels(
                "microsoft/IssueLens", 1, ["bug"]
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
