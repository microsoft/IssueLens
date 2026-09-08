import json
import os
import pathlib
import sys
import unittest
from typing import cast
from unittest.mock import patch

from mcp import Client


PACKAGE_ROOT = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp.config import ConfigurationError  # noqa: E402
from issuelens_github_mcp.github import GitHubClient  # noqa: E402
from issuelens_github_mcp.server import (  # noqa: E402
    build_server_from_environment,
    create_server,
    main,
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
    "get_pull_request",
    "list_pull_request_files",
    "list_pull_request_commits",
    "list_pull_request_reviews",
    "list_pull_request_review_comments",
    "get_commit",
    "compare_commits",
    "list_repository_tree",
    "search_repository_content",
    "list_merged_pull_requests",
    "get_wiki_snapshot",
    "list_wiki_pages",
    "get_wiki_page",
    "search_wiki",
    "list_wiki_history",
    "get_wiki_diff",
}
WRITE_TOOLS = {
    "add_labels",
    "set_assignees",
    "add_issue_comment",
    "add_eyes_reaction",
}


class FakeGitHubClient:
    def __init__(self, *, writes_enabled=False, wiki_writes_enabled=False):
        self.calls = []
        self.writes_enabled = writes_enabled
        self.wiki_writes_enabled = wiki_writes_enabled

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

    async def test_wiki_writer_has_only_direct_mutation_and_bounded_schema(self):
        github = FakeGitHubClient(wiki_writes_enabled=True)
        server = create_server(cast(GitHubClient, github))
        parameters = {
            "repository": "microsoft/IssueLens",
            "pages": {"Home.md": "Updated memory"},
            "expected_base": "a" * 40,
            "message": "Update memory",
        }

        async with Client(server) as client:
            tools = await client.list_tools()
            result = await client.call_tool("write_wiki_pages", parameters)

        self.assertEqual(
            {tool.name for tool in tools.tools}, READ_TOOLS | {"write_wiki_pages"}
        )
        tool = next(tool for tool in tools.tools if tool.name == "write_wiki_pages")
        self.assertEqual(set(tool.input_schema["properties"]), set(parameters))
        self.assertEqual(set(tool.input_schema["required"]), set(parameters))
        self.assertEqual(tool.input_schema["properties"]["pages"]["additionalProperties"], {"type": "string"})
        self.assertFalse(result.is_error)
        self.assertEqual(github.calls, [(
            "write_wiki_pages", tuple(parameters.values()), {},
        )])

    async def test_history_supports_snapshot_ref_and_retains_defaults(self):
        github = FakeGitHubClient()
        server = create_server(cast(GitHubClient, github))
        async with Client(server) as client:
            tools = await client.list_tools()
            default = await client.call_tool("list_wiki_history", {"repository": "microsoft/IssueLens"})
            pinned = await client.call_tool("list_wiki_history", {
                "repository": "microsoft/IssueLens", "path": "Home.md", "limit": 5, "ref": "a" * 40,
            })
        self.assertFalse(default.is_error)
        self.assertFalse(pinned.is_error)
        tool = next(tool for tool in tools.tools if tool.name == "list_wiki_history")
        self.assertEqual(tool.input_schema["properties"]["ref"]["default"], "HEAD")
        self.assertEqual(github.calls, [
            ("list_wiki_history", ("microsoft/IssueLens", None, 30, "HEAD"), {}),
            ("list_wiki_history", ("microsoft/IssueLens", "Home.md", 5, "a" * 40), {}),
        ])

    async def test_all_wiki_read_schemas_keep_source_repository_and_bounded_arguments(self):
        github = FakeGitHubClient()
        cases = {
            "get_wiki_snapshot": {},
            "list_wiki_pages": {"ref": "a" * 40},
            "get_wiki_page": {"path": "Home.md", "ref": "a" * 40},
            "search_wiki": {"query": "memory", "ref": "a" * 40},
            "list_wiki_history": {"path": "Home.md", "limit": 5, "ref": "a" * 40},
            "get_wiki_diff": {"base": "a" * 40, "head": "b" * 40},
        }
        async with Client(create_server(cast(GitHubClient, github))) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name, arguments in cases.items():
                with self.subTest(tool=name):
                    parameters = {"repository": "owner/source", **arguments}
                    self.assertEqual(set(tools[name].input_schema["properties"]), set(parameters))
                    self.assertIn("source project", tools[name].description)
                    self.assertIn("configured wiki", tools[name].description)
                    result = await client.call_tool(name, parameters)
                    self.assertFalse(result.is_error)
                    self.assertEqual(github.calls[-1], (name, tuple(parameters.values()), {}))

    async def test_get_file_forwarding_remains_unchanged(self):
        github = FakeGitHubClient()
        async with Client(create_server(cast(GitHubClient, github))) as client:
            result = await client.call_tool("get_file", {
                "repository": "microsoft/IssueLens", "path": "README.md", "ref": "main",
            })
        self.assertFalse(result.is_error)
        self.assertEqual(github.calls, [
            ("get_file", ("microsoft/IssueLens", "README.md"), {"ref": "main"}),
        ])

    async def test_wiki_write_tool_rejects_missing_or_wrong_typed_arguments(self):
        github = FakeGitHubClient(wiki_writes_enabled=True)
        valid = {
            "repository": "microsoft/IssueLens", "pages": {"Home.md": "text"},
            "expected_base": "a" * 40, "message": "Update",
        }
        invalid = [{key: value for key, value in valid.items() if key != "expected_base"}]
        invalid.extend({**valid, "pages": value} for value in ([], {"Home.md": None}, "text"))
        async with Client(create_server(cast(GitHubClient, github))) as client:
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    result = await client.call_tool("write_wiki_pages", arguments)
                    self.assertTrue(result.is_error)
        self.assertEqual(github.calls, [])

    async def test_reaction_tool_has_bounded_schema_and_round_trips(self):
        github = FakeGitHubClient(writes_enabled=True)
        server = create_server(cast(GitHubClient, github))

        async with Client(server) as client:
            tools = await client.list_tools()
            tool = next(
                item for item in tools.tools
                if item.name == "add_eyes_reaction"
            )
            result = await client.call_tool(
                "add_eyes_reaction",
                {
                    "repository": "microsoft/IssueLens",
                    "target_kind": "pull_request_review_comment",
                    "target_id": 99,
                },
            )

        self.assertEqual(
            set(tool.input_schema["required"]),
            {"repository", "target_kind", "target_id"},
        )
        self.assertEqual(
            set(tool.input_schema["properties"]["target_kind"]["enum"]),
            {
                "issue",
                "pull_request",
                "issue_comment",
                "pull_request_review_comment",
            },
        )
        self.assertNotIn("content", tool.input_schema["properties"])
        self.assertFalse(result.is_error)
        self.assertEqual(github.calls, [
            (
                "add_eyes_reaction",
                (
                    "microsoft/IssueLens",
                    "pull_request_review_comment",
                    99,
                ),
                {},
            )
        ])


class EnvironmentDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_wiki_writer_role_and_triage_flag_are_independent_and_lazy(self):
        for settings, wiki_writer, expected in (
            ({}, False, READ_TOOLS),
            ({"GITHUB_MCP_ENABLE_WRITES": "true"}, False, READ_TOOLS | WRITE_TOOLS),
            ({}, True, READ_TOOLS | {"write_wiki_pages"}),
            ({"GITHUB_MCP_ENABLE_WRITES": "true"}, True, READ_TOOLS | {"write_wiki_pages"}),
            ({"GITHUB_MCP_ENABLE_WRITES": "not-a-triage-process"}, True, READ_TOOLS | {"write_wiki_pages"}),
        ):
            with self.subTest(settings=settings, wiki_writer=wiki_writer):
                with patch("issuelens_github_mcp.server.GitHubAppTokenProvider") as provider:
                    server = build_server_from_environment({
                        "GITHUB_APP_ID": "1816975",
                        "GITHUB_APP_PRIVATE_KEY_SECRET_URI": "https://issuelens.vault.azure.net/secrets/not-read-at-startup",
                        **settings,
                    }, wiki_writer=wiki_writer)
                    async with Client(server) as client:
                        tools = await client.list_tools()
                    provider.return_value.get_token.assert_not_called()
                    provider.return_value.get_bot_identity.assert_not_called()
                self.assertEqual({tool.name for tool in tools.tools}, expected)


class EnvironmentTests(unittest.TestCase):
    def test_wiki_writer_role_requires_a_boolean(self):
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigurationError, "wiki_writer must be a boolean"):
                    build_server_from_environment({}, wiki_writer=value)

    def test_cli_selects_internal_role_without_a_wiki_environment_setting(self):
        for arguments, wiki_writer in (([], False), (["--wiki-writer"], True)):
            with self.subTest(arguments=arguments):
                with (
                    patch.object(sys, "argv", ["issuelens_github_mcp.server", *arguments]),
                    patch("issuelens_github_mcp.server.build_server_from_environment") as build,
                ):
                    main()
                    build.assert_called_once_with(wiki_writer=wiki_writer)
                    build.return_value.run.assert_called_once_with(transport="stdio")

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
