import unittest

import httpx
from copilot.tools import ToolInvocation

from related_github_tool import PublicGitHubClient, RelatedGitHubError, create_tool


class PublicClient:
    def __init__(self):
        self.calls = []

    async def execute(self, operation, repository, **arguments):
        self.calls.append((operation, repository, arguments))
        return [{"number": 10, "title": "Matching issue"}]


class RelatedGitHubToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_repository_read_is_delegated(self):
        public_client = PublicClient()
        tool = create_tool(public_client)

        result = await tool.handler(ToolInvocation(arguments={
            "repository": "redhat-developer/vscode-java",
            "operation": "search-issues",
            "query": '"error signature"',
        }))

        self.assertEqual(result.result_type, "success")
        self.assertEqual(
            public_client.calls[0][0:2],
            ("search-issues", "redhat-developer/vscode-java"),
        )
        self.assertIn("Matching issue", result.text_result_for_llm)

    async def test_public_client_error_maps_to_tool_failure(self):
        class FailingClient:
            async def execute(self, operation, repository, **arguments):
                raise RelatedGitHubError("public repository is inaccessible")

        tool = create_tool(FailingClient())

        result = await tool.handler(ToolInvocation(arguments={
            "repository": "octocat/Hello-World",
            "operation": "search-issues",
            "query": "error",
        }))

        self.assertEqual(result.result_type, "failure")
        self.assertIn("inaccessible", result.error)


class PublicGitHubClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_is_anonymous_and_repository_scoped(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"items": [{"number": 1}]})

        client = PublicGitHubClient(httpx.MockTransport(handler))
        result = await client.execute(
            "search-issues",
            "redhat-developer/vscode-java",
            query="NullPointerException",
        )

        self.assertEqual(result, [{"number": 1}])
        self.assertNotIn("Authorization", calls[0].headers)
        self.assertIn(
            "repo:redhat-developer/vscode-java is:issue",
            calls[0].url.params["q"],
        )

    async def test_rate_limit_has_explicit_failure(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            json={"message": "rate limit"},
        ))
        client = PublicGitHubClient(transport)

        with self.assertRaisesRegex(RelatedGitHubError, "rate limit exceeded"):
            await client.execute(
                "search-issues",
                "redhat-developer/vscode-java",
                query="error",
            )

    async def test_search_rejects_scope_qualifiers(self):
        client = PublicGitHubClient(httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": []})
        ))

        for query in ("repo:octocat/example error", "org:microsoft error"):
            with self.subTest(query=query):
                with self.assertRaisesRegex(
                    RelatedGitHubError,
                    "cannot set repository or owner scope",
                ):
                    await client.execute(
                        "search-issues",
                        "redhat-developer/vscode-java",
                        query=query,
                    )


if __name__ == "__main__":
    unittest.main()
