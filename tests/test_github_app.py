import importlib.util
import base64
import pathlib
import sys
import unittest
from unittest.mock import patch

import httpx
from copilot.tools import ToolInvocation


SCRIPT = (
    pathlib.Path(__file__).parents[1]
    / "skills"
    / "github-access"
    / "scripts"
    / "github_app.py"
)
SPEC = importlib.util.spec_from_file_location("github_app", SCRIPT)
github_app = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = github_app
SPEC.loader.exec_module(github_app)

TOOL_SCRIPT = SCRIPT.with_name("tool.py")
TOOL_SPEC = importlib.util.spec_from_file_location("github_access_tool", TOOL_SCRIPT)
github_access_tool = importlib.util.module_from_spec(TOOL_SPEC)
assert TOOL_SPEC and TOOL_SPEC.loader
sys.modules[TOOL_SPEC.name] = github_access_tool
TOOL_SPEC.loader.exec_module(github_access_tool)


class GitHubAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []

        def handler(request):
            self.calls.append(request)
            if request.url.path == "/repos/microsoft/IssueLens/installation":
                return httpx.Response(200, json={"id": 1234})
            if request.url.path == "/repos/microsoft/other/installation":
                return httpx.Response(200, json={"id": 1234})
            if request.url.path == "/app/installations/1234/access_tokens":
                return httpx.Response(
                    201,
                    json={
                        "token": "installation-secret",
                        "expires_at": "2030-01-01T01:00:00Z",
                    },
                )
            if request.url.path == "/repos/microsoft/IssueLens/issues":
                return httpx.Response(
                    200,
                    json=[
                        {"number": 1, "title": "Issue"},
                        {"number": 2, "pull_request": {}, "title": "PR"},
                    ],
                )
            if request.url.path == "/repos/microsoft/IssueLens/issues/1/reactions":
                return httpx.Response(200, json=[{"content": "+1"}])
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
                "/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
            ):
                return httpx.Response(
                    302,
                    headers={
                        "Location": (
                            "https://github-production-user-asset-1.s3.amazonaws.com/"
                            "123/image.png?signature=test"
                        )
                    },
                )
            if request.url.host == "github-production-user-asset-1.s3.amazonaws.com":
                return httpx.Response(
                    200,
                    content=b"\x89PNG\r\n\x1a\nimage bytes",
                    headers={"Content-Type": "image/png"},
                )
            if request.url.path == "/repos/microsoft/IssueLens/issues/1/labels":
                return httpx.Response(200, json=[{"name": "bug"}])
            return httpx.Response(404, json={"message": "Not Found"})

        self.transport = httpx.MockTransport(handler)
        self.provider = github_app.GitHubAppTokenProvider(
            github_app.GitHubAppConfig(app_id="1816975"),
            private_key_loader=self._load_key,
            transport=self.transport,
            clock=lambda: 1_700_000_000,
        )

    async def _load_key(self):
        return "test-key"

    @patch.object(github_app.jwt, "encode", return_value="app-jwt")
    async def test_token_is_resolved_and_cached_per_repository(self, _):
        first = await self.provider.get_installation_token("microsoft/IssueLens")
        second = await self.provider.get_installation_token("microsoft/IssueLens")

        self.assertEqual(first.token, "installation-secret")
        self.assertIs(first, second)
        self.assertEqual(len(self.calls), 2)

    async def test_repository_must_use_owner_repository_format(self):
        with self.assertRaisesRegex(github_app.GitHubAppError, "owner/repository"):
            await self.provider.get_installation_token("IssueLens")

    @patch.object(github_app.jwt, "encode", return_value="app-jwt")
    async def test_repositories_in_same_installation_share_one_token(self, _):
        await self.provider.get_installation_token("microsoft/IssueLens")
        second = await self.provider.get_installation_token("microsoft/other")

        self.assertEqual(second.installation_id, 1234)
        token_calls = [
            call
            for call in self.calls
            if call.url.path == "/app/installations/1234/access_tokens"
        ]
        self.assertEqual(len(token_calls), 1)

    @patch.object(github_app.jwt, "encode", return_value="app-jwt")
    async def test_list_issues_filters_pull_requests(self, _):
        client = github_app.GitHubAppClient(self.provider, self.transport)

        issues = await client.execute("list-issues", "microsoft/IssueLens")

        self.assertEqual([issue["number"] for issue in issues], [1])
        api_call = self.calls[-1]
        self.assertEqual(
            api_call.headers["Authorization"], "Bearer installation-secret"
        )
        self.assertEqual(api_call.url.params["sort"], "updated")

    @patch.object(github_app.jwt, "encode", return_value="app-jwt")
    async def test_list_reactions_supports_hot_issue_scoring(self, _):
        client = github_app.GitHubAppClient(self.provider, self.transport)

        reactions = await client.execute(
            "list-reactions", "microsoft/IssueLens", issue_number=1
        )

        self.assertEqual(reactions, [{"content": "+1"}])

    @patch.object(github_app.jwt, "encode", return_value="app-jwt")
    async def test_get_issue_images_downloads_allowlisted_image_without_leaking_token(self, _):
        client = github_app.GitHubAppClient(self.provider, self.transport)

        result = await client.execute(
            "get-issue-images", "microsoft/IssueLens", issue_number=1
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
            call for call in self.calls if call.url.host == "github.com"
        )
        redirect_call = next(
            call
            for call in self.calls
            if call.url.host == "github-production-user-asset-1.s3.amazonaws.com"
        )
        self.assertEqual(
            asset_call.headers["Authorization"], "Bearer installation-secret"
        )
        self.assertNotIn("Authorization", redirect_call.headers)

    async def test_issue_image_rejects_content_type_spoofing(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            content=b"not really a png",
            headers={"Content-Type": "image/png"},
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaisesRegex(
                github_app.GitHubAppError, "does not match"
            ):
                await github_app.GitHubAppClient._download_issue_image(
                    client,
                    "https://github.com/user-attachments/assets/"
                    "12345678-1234-1234-1234-123456789abc",
                    "installation-secret",
                    1024,
                )

    @patch.object(github_app.jwt, "encode", return_value="app-jwt")
    async def test_add_labels_uses_only_the_issue_labels_route(self, _):
        client = github_app.GitHubAppClient(self.provider, self.transport)

        result = await client.execute(
            "add-labels",
            "microsoft/IssueLens",
            issue_number=1,
            labels=["bug"],
        )

        self.assertEqual(result, [{"name": "bug"}])
        self.assertEqual(self.calls[-1].method, "POST")
        self.assertEqual(
            self.calls[-1].url.path,
            "/repos/microsoft/IssueLens/issues/1/labels",
        )

    async def test_unsupported_operations_fail_before_api_access(self):
        client = github_app.GitHubAppClient(self.provider, self.transport)
        with patch.object(self.provider, "get_installation_token") as get_token:
            with self.assertRaisesRegex(github_app.GitHubAppError, "Unsupported"):
                await client.execute("delete-repository", "microsoft/IssueLens")
            get_token.assert_not_called()

    async def test_get_file_rejects_windows_style_traversal(self):
        client = github_app.GitHubAppClient(self.provider, self.transport)

        with self.assertRaisesRegex(github_app.GitHubAppError, "relative path"):
            await client.execute(
                "get-file", "microsoft/IssueLens", path="..\\secret.pem"
            )

    @patch.object(github_app.jwt, "encode", return_value="app-jwt")
    async def test_http_not_found_preserves_status_code(self, _):
        client = github_app.GitHubAppClient(self.provider, self.transport)

        with self.assertRaises(github_app.GitHubAppError) as context:
            await client.execute(
                "get-file",
                "microsoft/IssueLens",
                path=".github/missing.md",
            )

        self.assertEqual(context.exception.status_code, 404)


class GitHubAccessToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_owned_tool_forwards_allowlisted_operation(self):
        calls = []

        class Client:
            async def execute(self, operation, repository, **arguments):
                calls.append((operation, repository, arguments))
                return {"number": 5, "title": "Issue"}

        tool = github_access_tool.create_tool(github_app, client=Client())

        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "github-access")
        self.assertIsNotNone(tool.handler)
        result = await tool.handler(ToolInvocation(arguments={
            "operation": "get-issue",
            "repository": "microsoft/IssueLens",
            "issue_number": 5,
        }))

        self.assertEqual(result.result_type, "success")
        self.assertEqual(result.text_result_for_llm, '{"number": 5, "title": "Issue"}')
        self.assertEqual(calls[0][0:2], ("get-issue", "microsoft/IssueLens"))
        self.assertEqual(calls[0][2]["issue_number"], 5)

    async def test_skill_owned_tool_maps_safe_client_error_to_failure(self):
        class Client:
            async def execute(self, operation, repository, **arguments):
                raise github_app.GitHubAppError("repository access denied")

        tool = github_access_tool.create_tool(github_app, client=Client())
        self.assertIsNotNone(tool)
        self.assertIsNotNone(tool.handler)

        result = await tool.handler(ToolInvocation(arguments={
            "operation": "get-repository",
            "repository": "microsoft/IssueLens",
        }))

        self.assertEqual(result.result_type, "failure")
        self.assertEqual(result.error, "repository access denied")
        self.assertNotIn("token", result.text_result_for_llm.lower())

    def test_skill_owned_tool_is_absent_without_app_configuration(self):
        with patch.object(
            github_app.GitHubAppTokenProvider,
            "from_environment",
            side_effect=github_app.GitHubAppError("not configured"),
        ):
            tool = github_access_tool.create_tool(github_app)

        self.assertIsNone(tool)


if __name__ == "__main__":
    unittest.main()
