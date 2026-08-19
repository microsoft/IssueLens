import json
import os
import pathlib
import sys
import unittest
from typing import cast

from mcp import Client


PACKAGE_ROOT = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp.config import ConfigurationError  # noqa: E402
from issuelens_github_mcp.github import GitHubClient  # noqa: E402
from issuelens_github_mcp.server import (  # noqa: E402
    build_server_from_environment,
    create_server,
)


READ_TOOLS = {
    "get_repository",
    "list_issues",
    "get_issue",
    "list_issue_comments",
    "get_issue_comment",
    "list_issue_reactions",
    "search_issues",
    "list_labels",
    "get_file",
}
WRITE_TOOLS = {
    "add_labels",
    "set_assignees",
    "add_issue_comment",
    "add_eyes_reaction",
}


class FakeGitHubClient:
    def __init__(self, *, writes_enabled=False):
        self.calls = []
        self.writes_enabled = writes_enabled

    def __getattr__(self, operation):
        async def call(*args, **kwargs):
            self.calls.append((operation, args, kwargs))
            return {"operation": operation, "arguments": list(args)}

        return call


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_server_discovers_only_bounded_read_tools(self):
        server = create_server(cast(GitHubClient, FakeGitHubClient()))

        async with Client(server) as client:
            result = await client.list_tools()

        self.assertEqual({tool.name for tool in result.tools}, READ_TOOLS)
        for tool in result.tools:
            self.assertIn("repository", tool.input_schema["required"])

    async def test_tool_call_round_trips_through_mcp_protocol(self):
        github = FakeGitHubClient()
        server = create_server(cast(GitHubClient, github))

        async with Client(server) as client:
            result = await client.call_tool(
                "get_issue",
                {"repository": "microsoft/IssueLens", "issue_number": 7},
            )

        self.assertFalse(result.is_error)
        self.assertEqual(github.calls, [
            (
                "get_issue",
                ("microsoft/IssueLens", 7),
                {},
            )
        ])
        text_content = getattr(result.content[0], "text", None)
        self.assertIsInstance(text_content, str)
        self.assertEqual(
            json.loads(cast(str, text_content))["operation"],
            "get_issue",
        )

    async def test_exact_comment_tool_round_trips_issue_and_comment_ids(self):
        github = FakeGitHubClient()
        server = create_server(cast(GitHubClient, github))

        async with Client(server) as client:
            result = await client.call_tool(
                "get_issue_comment",
                {
                    "repository": "microsoft/IssueLens",
                    "issue_number": 14,
                    "comment_id": 99,
                },
            )

        self.assertFalse(result.is_error)
        self.assertEqual(github.calls, [
            (
                "get_issue_comment",
                ("microsoft/IssueLens", 14, 99),
                {},
            )
        ])

    async def test_write_tools_are_registered_only_when_enabled(self):
        server = create_server(cast(
            GitHubClient,
            FakeGitHubClient(writes_enabled=True),
        ))

        async with Client(server) as client:
            result = await client.list_tools()

        self.assertEqual(
            {tool.name for tool in result.tools},
            READ_TOOLS | WRITE_TOOLS,
        )

    async def test_eyes_reaction_tool_has_fixed_typed_target(self):
        github = FakeGitHubClient(writes_enabled=True)
        server = create_server(cast(GitHubClient, github))

        async with Client(server) as client:
            result = await client.call_tool(
                "add_eyes_reaction",
                {
                    "repository": "microsoft/IssueLens",
                    "subject_type": "pull_request",
                    "subject_number": 21,
                    "comment_id": 987,
                },
            )

        self.assertFalse(result.is_error)
        self.assertEqual(github.calls, [
            (
                "add_eyes_reaction",
                ("microsoft/IssueLens", "pull_request", 21, 987),
                {},
            )
        ])


class EnvironmentTests(unittest.TestCase):
    def test_write_flag_must_be_boolean(self):
        with self.assertRaisesRegex(ConfigurationError, "true or false"):
            build_server_from_environment({
                "GITHUB_APP_ID": "1816975",
                "GITHUB_APP_PRIVATE_KEY_SECRET_URI": (
                    "https://issuelens.vault.azure.net/secrets/github-app-key"
                ),
                "GITHUB_MCP_ENABLE_WRITES": "sometimes",
            })


if __name__ == "__main__":
    unittest.main()
