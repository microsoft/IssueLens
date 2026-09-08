import os
import pathlib
import subprocess
import sys
import tempfile
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
    def environment(self, **overrides):
        return {
            **os.environ,
            "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GITHUB_APP_ID": "1816975",
            "GITHUB_APP_PRIVATE_KEY_SECRET_URI": (
                "https://issuelens.vault.azure.net/secrets/not-read-at-startup"
            ),
            "GITHUB_MCP_ENABLE_WRITES": "false",
            "GITHUB_MCP_WIKI_WRITE_REPOSITORIES": "",
            **overrides,
        }

    def test_package_imports_without_repository_root_on_pythonpath(self):
        source = self.environment()["PYTHONPATH"]
        code = (
            "import pathlib, sys; "
            "from issuelens_github_mcp import auth, github, server, wiki; "
            "source = pathlib.Path(sys.argv[1]).resolve(); "
            "assert all(pathlib.Path(module.__file__).resolve().is_relative_to(source) "
            "for module in (auth, github, server, wiki)); "
            "assert 'wiki' not in sys.modules; "
            "assert str(source.parent.parent) not in sys.path; "
            "print('package-only imports verified')"
        )
        with tempfile.TemporaryDirectory(prefix="wiki-stdio-import-") as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-P", "-c", code, source],
                cwd=directory, env=self.environment(), capture_output=True,
                text=True, timeout=30, check=True,
            )
        self.assertEqual(result.stdout.strip(), "package-only imports verified")

    async def assert_discovery(self, overrides, expected):
        with tempfile.TemporaryDirectory(prefix="wiki-stdio-") as directory:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-P", "-m", "issuelens_github_mcp.server"],
                env=self.environment(**overrides), cwd=pathlib.Path(directory),
            )
            async with Client(stdio_client(parameters), mode="legacy") as client:
                tools = await client.list_tools()
        self.assertEqual({tool.name for tool in tools.tools}, expected)

    async def test_read_only_stdio_discovery_without_secret_access(self):
        await self.assert_discovery({}, READ_TOOLS)

    async def test_triage_stdio_discovery_excludes_wiki_writes(self):
        await self.assert_discovery(
            {"GITHUB_MCP_ENABLE_WRITES": "true"},
            READ_TOOLS | {"add_labels", "set_assignees", "add_issue_comment", "add_eyes_reaction"},
        )

    async def test_wiki_writer_stdio_discovery_excludes_triage_writes(self):
        await self.assert_discovery(
            {"GITHUB_MCP_WIKI_WRITE_REPOSITORIES": "microsoft/IssueLens"},
            READ_TOOLS | {"write_wiki_pages"},
        )


if __name__ == "__main__":
    unittest.main()
