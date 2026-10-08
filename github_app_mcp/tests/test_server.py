import asyncio
import json
import os
import pathlib
import sys
import unittest
from typing import cast
from unittest.mock import AsyncMock, patch

from mcp import Client


PACKAGE_ROOT = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp.config import ConfigurationError  # noqa: E402
from issuelens_github_mcp.auth import GitHubAppError  # noqa: E402
from issuelens_github_mcp.github import GitHubClient  # noqa: E402
from issuelens_github_mcp.outcomes import ERROR_TYPES, ToolFailure, failure_result  # noqa: E402
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
    def test_every_error_type_has_static_telemetry_message(self):
        for error_type in ERROR_TYPES | {"PRIVATE-CANARY"}:
            with self.subTest(error_type=error_type):
                result = failure_result(ToolFailure(
                    "Repository content: PRIVATE-CANARY",
                    error_type=error_type,
                    http_status=503,
                ))
                details = result["error"]
                self.assertIsNotNone(details)
                self.assertNotIn("PRIVATE-CANARY", details["telemetry_message"])
                self.assertIn("HTTP 503", details["telemetry_message"])

    async def test_every_tool_returns_one_structured_execution_outcome(self):
        github = FakeGitHubClient(writes_enabled=True, wiki_writes_enabled=True)
        values = {
            "repository": "microsoft/IssueLens", "issue_number": 7, "pull_number": 7,
            "comment_id": 8, "query": "memory", "path": "Home.md", "sha": "a" * 40,
            "ref": "a" * 40, "base": "a" * 40, "head": "b" * 40,
            "expected_base": "a" * 40, "expected_wiki_repository": "microsoft/IssueLens",
            "pages": {"Home.md": "Memory"}, "message": "Update memory",
            "labels": ["bug"], "assignees": ["octocat"], "body": "Comment",
            "target_kind": "issue", "target_id": 7,
        }
        async with Client(create_server(cast(GitHubClient, github))) as client:
            tools = await client.list_tools()
            for tool in tools.tools:
                with self.subTest(tool=tool.name):
                    arguments = {key: values[key] for key in tool.input_schema["required"]}
                    before = len(github.calls)
                    response = await client.call_tool(tool.name, arguments)
                    envelope = json.loads(response.content[0].text)
                    self.assertFalse(response.is_error, response.content)
                    self.assertEqual(response.structured_content, envelope)
                    self.assertEqual(set(envelope), {"success", "outcome", "result", "error"})
                    self.assertTrue(envelope["success"])
                    self.assertEqual(envelope["outcome"], "completed")
                    self.assertIsNone(envelope["error"])
                    self.assertEqual(envelope["result"]["operation"], tool.name)
                    self.assertEqual(len(github.calls), before + 1)

    async def test_failed_execution_keeps_safe_type_message_status_and_native_failure(self):
        for outcome in ("not_applied", "unknown"):
            github = FakeGitHubClient(writes_enabled=True)
            github.add_issue_comment = AsyncMock(side_effect=GitHubAppError(
                "GitHub API returned HTTP 503", error_type="upstream_error",
                http_status=503, outcome=outcome,
            ))
            async with Client(create_server(cast(GitHubClient, github))) as client:
                response = await client.call_tool("add_issue_comment", {
                    "repository": "microsoft/IssueLens", "issue_number": 7, "body": "Comment",
                })
            self.assertTrue(response.is_error)
            envelope = json.loads(response.content[0].text)
            self.assertEqual(envelope, {
                "success": False, "outcome": outcome, "result": None,
                "error": {
                    "type": "upstream_error",
                    "message": "GitHub API returned HTTP 503",
                    "telemetry_message": "Upstream service request failed (HTTP 503).",
                    "http_status": 503,
                },
            })
            self.assertEqual(response.structured_content, envelope)
            github.add_issue_comment.assert_awaited_once()

    async def test_schema_failure_is_not_applied_and_does_not_echo_input(self):
        github = FakeGitHubClient()
        async with Client(create_server(cast(GitHubClient, github))) as client:
            response = await client.call_tool("get_commit", {
                "repository": "microsoft/IssueLens", "sha": "a" * 40,
                "per_page": "PRIVATE-CANARY",
            })
        envelope = json.loads(response.content[0].text)
        self.assertTrue(response.is_error)
        self.assertEqual(envelope["error"]["type"], "invalid_input")
        self.assertEqual(envelope["outcome"], "not_applied")
        self.assertNotIn("PRIVATE-CANARY", str(response))
        self.assertEqual(github.calls, [])

    async def test_disabled_write_is_not_applied_without_calling_the_backend(self):
        github = FakeGitHubClient()
        async with Client(create_server(cast(GitHubClient, github))) as client:
            response = await client.call_tool("write_wiki_pages", {})
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["outcome"], "not_applied")
        self.assertEqual(response.structured_content["error"]["type"], "permission_denied")
        self.assertEqual(github.calls, [])

    async def test_unhandled_write_errors_are_unknown_safe_and_not_retried(self):
        github = FakeGitHubClient(writes_enabled=True)
        github.add_issue_comment = AsyncMock(side_effect=OSError("SECRET-URL?sig=PRIVATE-CANARY"))
        async with Client(create_server(cast(GitHubClient, github))) as client:
            response = await client.call_tool("add_issue_comment", {
                "repository": "microsoft/IssueLens", "issue_number": 7, "body": "Comment",
            })
        envelope = json.loads(response.content[0].text)
        self.assertTrue(response.is_error)
        self.assertEqual(envelope["error"]["type"], "internal_error")
        self.assertEqual(envelope["outcome"], "unknown")
        self.assertNotIn("PRIVATE-CANARY", str(response))
        github.add_issue_comment.assert_awaited_once()

    async def test_empty_results_remain_successful_and_envelope_is_bounded(self):
        github = FakeGitHubClient()
        github.list_issues = AsyncMock(return_value=[])
        server = create_server(cast(GitHubClient, github))
        response = await server.call_tool("list_issues", {"repository": "microsoft/IssueLens"})
        self.assertEqual(response.structured_content["result"], [])
        self.assertTrue(response.structured_content["success"])
        with patch("issuelens_github_mcp.server._MAX_RESULT_BYTES", 1):
            response = await server.call_tool("list_issues", {"repository": "microsoft/IssueLens"})
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["error"]["type"], "limit_exceeded")

    async def test_cancellation_is_not_converted_to_success_or_retried(self):
        github = FakeGitHubClient()
        github.get_issue = AsyncMock(side_effect=asyncio.CancelledError)
        server = create_server(cast(GitHubClient, github))
        with self.assertRaises(asyncio.CancelledError):
            await server.call_tool("get_issue", {"repository": "microsoft/IssueLens", "issue_number": 7})
        github.get_issue.assert_awaited_once()

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
            json.loads(cast(str, text_content))["result"]["operation"],
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

    async def test_commit_detail_and_pagination_are_discoverable_and_forwarded(self):
        github = FakeGitHubClient()
        server = create_server(cast(GitHubClient, github))
        async with Client(server) as client:
            tools = await client.list_tools()
            default = await client.call_tool("get_commit", {
                "repository": "microsoft/IssueLens", "sha": "a" * 40,
            })
            full = await client.call_tool("get_commit", {
                "repository": "microsoft/IssueLens", "sha": "a" * 40,
                "detail": "full_patch", "per_page": 1, "page": 101,
            })
        self.assertFalse(default.is_error)
        self.assertFalse(full.is_error)
        tool = next(tool for tool in tools.tools if tool.name == "get_commit")
        properties = tool.input_schema["properties"]
        self.assertEqual(properties["detail"]["default"], "stats")
        self.assertEqual(set(properties["detail"]["enum"]), {"none", "stats", "full_patch"})
        self.assertEqual(properties["per_page"]["maximum"], 100)
        self.assertEqual(properties["page"]["maximum"], 3000)
        self.assertEqual(github.calls, [
            ("get_commit", ("microsoft/IssueLens", "a" * 40), {
                "detail": "stats", "per_page": 30, "page": 1,
            }),
            ("get_commit", ("microsoft/IssueLens", "a" * 40), {
                "detail": "full_patch", "per_page": 1, "page": 101,
            }),
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
            "expected_wiki_repository": "microsoft/TeamMemory",
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
        self.assertEqual(tool.input_schema["properties"]["expected_wiki_repository"]["type"], "string")
        self.assertIn("precondition, never a destination override", tool.description)
        self.assertFalse(result.is_error)
        self.assertEqual(github.calls, [(
            "write_wiki_pages",
            (parameters["repository"], parameters["pages"], parameters["expected_base"], parameters["message"]),
            {"expected_wiki_repository": parameters["expected_wiki_repository"]},
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
                    if name in {"list_wiki_pages", "search_wiki", "list_wiki_history", "get_wiki_diff"}:
                        for field in ("source_repository", "wiki_repository", "result"):
                            self.assertIn(field, tools[name].description)
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
            "expected_wiki_repository": "microsoft/TeamMemory",
        }
        invalid = [
            {key: value for key, value in valid.items() if key != missing}
            for missing in ("expected_base", "expected_wiki_repository")
        ]
        invalid.extend({**valid, "pages": value} for value in ([], {"Home.md": None}, "text"))
        invalid.extend(
            {**valid, "expected_wiki_repository": value}
            for value in (None, 1, True, [], {})
        )
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
