import os
import pathlib
import sys
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client


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


class StdioServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_module_completes_stdio_handshake_without_secret_access(self):
        root = pathlib.Path(__file__).parents[2]
        environment = {
            **os.environ,
            "GITHUB_APP_ID": "1816975",
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI": (
                "https://issuelens.vault.azure.net/secrets/not-read-at-startup"
            ),
            "GITHUB_MCP_ENABLE_WRITES": "false",
        }
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "issuelens_github_mcp.server"],
            env=environment,
            cwd=root,
        )

        async with Client(stdio_client(parameters), mode="legacy") as client:
            tools = await client.list_tools()

        self.assertEqual({tool.name for tool in tools.tools}, READ_TOOLS)


if __name__ == "__main__":
    unittest.main()
