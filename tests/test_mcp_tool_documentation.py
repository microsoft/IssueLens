import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "github_app_mcp" / "src" / "issuelens_github_mcp"
REST_READ_PERMISSIONS = {
    "get_repository": "metadata",
    "list_issues": "issues",
    "get_issue": "issues",
    "list_issue_comments": "issues",
    "get_issue_comment": "issues",
    "list_issue_reactions": "issues",
    "search_issues": "issues",
    "list_labels": "issues",
    "get_file": "contents",
    "get_pull_request": "pull_requests",
    "list_pull_request_files": "pull_requests",
    "list_pull_request_commits": "pull_requests",
    "list_pull_request_reviews": "pull_requests",
    "list_pull_request_review_comments": "pull_requests",
    "get_commit": "contents",
    "compare_commits": "contents",
    "list_repository_tree": "contents",
    "search_repository_content": "contents",
    "list_merged_pull_requests": "pull_requests",
}
PERMISSION_LABELS = {
    "metadata": "Metadata: read",
    "issues": "Issues: read",
    "contents": "Contents: read",
    "pull_requests": "Pull requests: read",
}
WIKI_READ_TOOLS = {
    "get_wiki_snapshot", "list_wiki_pages", "get_wiki_page",
    "search_wiki", "list_wiki_history", "get_wiki_diff",
}
WRITE_TOOLS = {
    "add_labels", "set_assignees", "add_issue_comment", "add_eyes_reaction",
    "write_wiki_pages",
}


def registered_tool_names(nodes):
    return {
        node.name
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "server"
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
    }


class MCPToolDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = ast.parse((PACKAGE / "server.py").read_text(encoding="utf-8"))
        cls.server_factory = next(
            node for node in server.body
            if isinstance(node, ast.FunctionDef) and node.name == "create_server"
        )
        client = ast.parse((PACKAGE / "github.py").read_text(encoding="utf-8"))
        client_class = next(
            node for node in client.body
            if isinstance(node, ast.ClassDef) and node.name == "GitHubClient"
        )
        cls.client_methods = {
            node.name: node for node in client_class.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        cls.readme_lines = (ROOT / "github_app_mcp" / "README.md").read_text(
            encoding="utf-8"
        ).splitlines()

    def read_table(self, header):
        start = self.readme_lines.index(header)
        column_count = len(header.strip("|").split("|"))
        rows = {}
        for line in self.readme_lines[start + 2:]:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            self.assertEqual(len(cells), column_count, line)
            tool_name = cells[0].strip("`")
            self.assertNotIn(tool_name, rows, "Duplicate documented tool")
            rows[tool_name] = tuple(cells[1:])
        return rows

    def test_rest_table_covers_registered_reads_with_minimal_permissions(self):
        registered = registered_tool_names(ast.walk(self.server_factory))
        rest_reads = registered - WIKI_READ_TOOLS - WRITE_TOOLS
        documented = self.read_table("| Tool | Preferred App permission |")
        self.assertEqual(set(documented), rest_reads)
        self.assertEqual(documented, {
            tool: (PERMISSION_LABELS[permission],)
            for tool, permission in REST_READ_PERMISSIONS.items()
        })

    def test_rest_endpoint_permissions_match_expected_contract(self):
        for tool, permission in REST_READ_PERMISSIONS.items():
            with self.subTest(tool=tool):
                requests = [
                    node for node in ast.walk(self.client_methods[tool])
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and node.func.attr == "_request"
                ]
                self.assertTrue(requests, "Expected a bounded REST request")
                for request in requests:
                    self.assertEqual(ast.literal_eval(request.args[0]), "GET")
                    permissions = next(
                        keyword.value for keyword in request.keywords
                        if keyword.arg == "permissions"
                    )
                    self.assertEqual(
                        ast.literal_eval(permissions), {permission: "read"}
                    )

    def test_shared_reads_separate_wiki_tools_from_gated_writes(self):
        shared_reads = registered_tool_names(self.server_factory.body)
        self.assertEqual(shared_reads, set(REST_READ_PERMISSIONS) | WIKI_READ_TOOLS)
        registered = registered_tool_names(ast.walk(self.server_factory))
        self.assertEqual(registered - shared_reads, WRITE_TOOLS)
        documented = self.read_table("| Tool | Purpose | Required App permission |")
        self.assertEqual(set(documented), WIKI_READ_TOOLS)
        for tool, (purpose, permission) in documented.items():
            with self.subTest(tool=tool):
                self.assertTrue(purpose)
                self.assertEqual(permission, "Contents: read")


if __name__ == "__main__":
    unittest.main()
