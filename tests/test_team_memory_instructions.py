import ast
import json
import pathlib
import unittest

import yaml
from copilot import CopilotClient
from copilot.tools import ToolInvocation

from issuelens_config_tool import create_tool


ROOT = pathlib.Path(__file__).parents[1]


class PolicyClient:
    def __init__(self, files):
        self.files = files
        self.calls = []

    async def get_file(self, repository, path):
        self.calls.append((repository, path))
        if path == ".github":
            return [
                {"name": "issuelens.yml", "type": "file"}
            ] if ".github/issuelens.yml" in self.files else []
        if path not in self.files:
            raise RuntimeError("HTTP 404")
        return {"type": "file", "decoded_content": self.files[path]}


class TeamMemoryPolicyToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_policy_round_trips_through_real_config_tool(self):
        policy = (ROOT / "examples" / "team-memory.md").read_text(encoding="utf-8")
        client = PolicyClient({
            ".github/issuelens.yml": (
                "version: 1\ninstructions:\n"
                "  team_memory:\n    path: .github/issuelens/team-memory.md\n"
                "  planning:\n    path: .github/issuelens/planning.md\n"
            ),
            ".github/issuelens/team-memory.md": policy,
            ".github/issuelens/planning.md": "Unrelated planning policy.",
        })
        tool = create_tool(client)
        self.assertIn("team_memory", tool.parameters["properties"]["domain"]["enum"])
        result = await tool.handler(ToolInvocation(arguments={
            "repository": "microsoft/IssueLens", "domain": "team_memory",
        }))
        self.assertEqual(result.result_type, "success")
        payload = json.loads(result.text_result_for_llm)
        self.assertEqual(payload["source"], "configured")
        self.assertEqual(payload["content"], policy)
        self.assertNotIn("Unrelated planning policy", result.text_result_for_llm)
        self.assertNotIn(
            ("microsoft/IssueLens", ".github/issuelens/planning.md"), client.calls
        )

    async def test_absent_or_omitted_policy_uses_built_in_behavior(self):
        for files in ({}, {".github/issuelens.yml": "version: 1\ninstructions: {}\n"}):
            with self.subTest(files=files):
                result = await create_tool(PolicyClient(files)).handler(ToolInvocation(
                    arguments={"repository": "microsoft/IssueLens", "domain": "team_memory"}
                ))
                self.assertEqual(result.result_type, "success")
                payload = json.loads(result.text_result_for_llm)
                self.assertEqual(payload["source"], "built-in")
                self.assertIsNone(payload["content"])

    async def test_invalid_or_missing_policy_fails_closed(self):
        for config in (
            "version: 2\n",
            "version: 1\ninstructions:\n  team_memory:\n    path: ../policy.md\n",
            "version: 1\ninstructions:\n  team_memory:\n    path: missing.md\n",
        ):
            with self.subTest(config=config):
                tool = create_tool(PolicyClient({".github/issuelens.yml": config}))
                result = await tool.handler(ToolInvocation(arguments={
                    "repository": "microsoft/IssueLens", "domain": "team_memory",
                }))
                self.assertEqual(result.result_type, "failure")
                self.assertIn("IssueLens configuration failed", result.text_result_for_llm)


class TeamMemoryInstructionTests(unittest.TestCase):
    def test_every_registered_agent_preloads_policy_and_reader(self):
        module = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        agents = {}
        for node in module.body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id.endswith("_AGENT")
                and isinstance(node.value, ast.Dict)
            ):
                fields = {
                    key.value: value
                    for key, value in zip(node.value.keys, node.value.values)
                    if isinstance(key, ast.Constant)
                }
                agents[node.target.id] = ast.literal_eval(fields["skills"])
        self.assertEqual(len(agents), 5)
        for name, skills in agents.items():
            with self.subTest(agent=name):
                self.assertIn("issuelens-config", skills)
                self.assertIn("team-memory", skills)

    def test_writer_requires_validated_policy_and_confirmed_publication(self):
        prompt = (ROOT / "agents" / "team-memory.md").read_text(encoding="utf-8")
        self.assertIn('domain="team_memory"', prompt)
        self.assertIn("call the `issuelens-config` tool", prompt)
        self.assertIn("returned `content`", prompt)
        self.assertIn("`wiki_repository`", prompt)
        self.assertIn("stops maintenance without silent source-wiki fallback", prompt)
        self.assertIn("priority knowledge areas", prompt)
        self.assertIn("Wiki writes belong to this maintenance job", prompt)
        self.assertIn("publication result confirms the wiki commit", prompt)
        self.assertIn("wiki was not updated", prompt)

    def test_writer_docs_bind_both_preconditions_to_the_read_snapshot(self):
        for path in (
            "agents/team-memory.md", "README.md", "github_app_mcp/README.md",
            ".github/issuelens/team-memory.md", "examples/team-memory.md",
        ):
            with self.subTest(path=path):
                text = " ".join((ROOT / path).read_text(encoding="utf-8").split())
                self.assertIn("expected_wiki_repository=read_snapshot.wiki_repository", text)
                self.assertIn("expected_base=read_snapshot.sha", text)
                self.assertIn("precondition, never a destination override", text)
                self.assertIn("even if the SHA is unchanged", text)
                self.assertIn("read a fresh snapshot", text.lower())

    def test_reader_is_discoverable_and_read_only(self):
        prompt = (ROOT / "skills" / "team-memory" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        frontmatter = yaml.safe_load(prompt.split("---", 2)[1])
        self.assertEqual(frontmatter["name"], "team-memory")
        self.assertIn("Read-only", frontmatter["description"])
        self.assertIn('domain="team_memory"', prompt)
        self.assertIn("stops wiki retrieval", prompt)
        self.assertIn("returned\n`content`", prompt)
        self.assertIn("returned `wiki_repository`", prompt)
        self.assertIn("must not call `write_wiki_pages`", prompt)
        self.assertIn("same snapshot", prompt)
        self.assertIn("Do not delegate ordinary retrieval", prompt)

    def test_config_skill_documents_memory_domain_and_boundaries(self):
        prompt = (ROOT / "skills" / "issuelens-config" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- `team_memory`", prompt)
        self.assertIn("inclusion/exclusion guidance", prompt)
        self.assertIn("Policy grants no independent write permission", prompt)

    def test_orchestrator_routes_maintenance_not_shared_retrieval(self):
        prompt = (ROOT / "agents" / "issuelens.md").read_text(encoding="utf-8")
        self.assertIn("planning, and team-memory capabilities", prompt)
        self.assertIn("do not dispatch routine\nretrieval", prompt)
        self.assertIn("Retrieval cannot publish wiki", prompt)
        self.assertIn("write_wiki_pages", prompt)
        self.assertIn("not stored approval state", prompt)

    def test_database_and_root_git_helpers_are_removed(self):
        self.assertFalse((ROOT / "team_memory.py").exists())
        self.assertFalse((ROOT / "wiki.py").exists())
        self.assertTrue(
            (ROOT / "github_app_mcp" / "src" / "issuelens_github_mcp" / "wiki.py").is_file()
        )


class TeamMemoryAccessTests(unittest.TestCase):
    def setUp(self):
        module = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        selected = [
            node for node in module.body
            if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_TEAM_MEMORY_AGENT")
            or (isinstance(node, ast.FunctionDef) and node.name in {
                "_configured_team_memory_agent", "_session_options",
            })
        ]
        self.namespace = {
            "CustomAgentConfig": dict,
            "Tool": object,
            "_agents_dir": ROOT / "agents",
            "_load_prompt": lambda path: path.read_text(encoding="utf-8"),
            "_ISSUELENS_AGENT": {"name": "issuelens"},
            "_TRIAGE_AGENT": {"name": "triage"},
            "_FIND_CRITICALS_AGENT": {"name": "find-criticals"},
            "_PLAN_AGENT": {"name": "plan"},
            "_RUNTIME_TOOLS": [],
            "_working_dir": "test-workdir",
            "_skills_dir": "test-skills",
            "_byok_provider": lambda: (None, "test-model"),
            "PermissionHandler": type("PermissionHandler", (), {"approve_all": None}),
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), "main.py", "exec"), self.namespace)
        self.agent = self.namespace["_TEAM_MEMORY_AGENT"]
        self.servers = {"github": {
            "type": "stdio", "command": "python", "args": ["-m", "issuelens_github_mcp.server"],
            "tools": ["*"],
            "env": {"GITHUB_MCP_ENABLE_WRITES": "true"},
        }}

    def test_maintenance_allowlist_is_explicit_on_sdk_wire(self):
        expected = {
            "issuelens-config",
            "github-get_repository", "github-list_issues", "github-get_issue",
            "github-list_issue_comments", "github-get_issue_comment", "github-search_issues",
            "github-get_file", "github-get_pull_request", "github-list_pull_request_files",
            "github-list_pull_request_commits", "github-list_pull_request_reviews",
            "github-list_pull_request_review_comments", "github-get_commit",
            "github-compare_commits", "github-list_repository_tree",
            "github-search_repository_content", "github-list_merged_pull_requests",
            "github-get_wiki_snapshot", "github-list_wiki_pages", "github-get_wiki_page",
            "github-search_wiki", "github-list_wiki_history", "github-get_wiki_diff",
            "wiki-writer-write_wiki_pages",
        }
        client = CopilotClient.__new__(CopilotClient)
        for servers in (self.servers, {}):
            with self.subTest(github_configured=bool(servers)):
                options = self.namespace["_session_options"](servers)
                agent = next(agent for agent in options["custom_agents"] if agent["name"] == "team-memory")
                self.assertIn("tools", agent)
                self.assertEqual(set(agent["tools"]), expected)
                self.assertEqual(len(agent["tools"]), len(expected))
                wire = client._convert_custom_agent_to_wire_format(agent)
                self.assertEqual(set(wire["tools"]), expected)
                for other in options["custom_agents"]:
                    if other["name"] != "team-memory":
                        self.assertNotIn("tools", other)

    def test_maintenance_allowlist_uses_registered_reads_and_only_wiki_write(self):
        module = ast.parse((ROOT / "github_app_mcp" / "src" / "issuelens_github_mcp" / "server.py").read_text(encoding="utf-8"))
        factory = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "create_server")
        reads = {node.name for node in factory.body if isinstance(node, ast.AsyncFunctionDef)}
        allowed = self.agent.get("tools", [])
        self.assertTrue(allowed)
        for name in allowed:
            if name.startswith("github-"):
                self.assertIn(name.removeprefix("github-"), reads)
            else:
                self.assertIn(name, {"issuelens-config", "wiki-writer-write_wiki_pages"})
        self.assertTrue(set(allowed).isdisjoint({
            "*", "github-*", "wiki-writer-*", "send-email", "send-teams-notification",
            "github-add_labels", "github-set_assignees", "github-add_issue_comment",
            "github-add_eyes_reaction", "github-write_wiki_pages",
        }))

    def test_writer_is_agent_local_without_mutating_shared_server(self):
        options = self.namespace["_session_options"](self.servers)
        for agent in options["custom_agents"]:
            if agent["name"] == "team-memory":
                writer = agent["mcp_servers"]["wiki-writer"]
                self.assertEqual(writer["tools"], ["write_wiki_pages"])
                self.assertEqual(writer["env"]["GITHUB_MCP_ENABLE_WRITES"], "false")
                self.assertEqual(writer["args"], ["-m", "issuelens_github_mcp.server", "--wiki-writer"])
            else:
                self.assertNotIn("mcp_servers", agent)
        self.assertIs(options["mcp_servers"], self.servers)
        self.assertNotIn("--wiki-writer", self.servers["github"]["args"])
        self.assertNotIn("mcp_servers", self.agent)

    def test_writer_is_not_available_without_github_server(self):
        options = self.namespace["_session_options"]({})
        self.assertTrue(all("mcp_servers" not in agent for agent in options["custom_agents"]))

    def test_shared_server_does_not_launch_writer_mode(self):
        module = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_github_mcp_server")
        arguments = next(
            value for node in ast.walk(function) if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "args"
        )
        self.assertEqual(ast.literal_eval(arguments), ["-m", "issuelens_github_mcp.server"])

    def test_no_wiki_repository_environment_configuration(self):
        agent = yaml.safe_load((ROOT / "agent.yaml").read_text(encoding="utf-8"))
        azure = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
        for entries in (
            agent["environment_variables"],
            azure["services"]["IssueLens"]["environmentVariables"],
        ):
            variables = {entry["name"]: entry["value"] for entry in entries}
            self.assertNotIn("ISSUELENS_WIKI_WRITE_REPOSITORIES", variables)
            self.assertNotIn("GITHUB_MCP_WIKI_WRITE_REPOSITORIES", variables)
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("WIKI_WRITE_REPOSITORIES", main_source)

    def test_actual_project_customization_names_its_wiki(self):
        config = yaml.safe_load((ROOT / ".github" / "issuelens.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            config["instructions"]["team_memory"]["wiki_repository"], "microsoft/IssueLens"
        )


if __name__ == "__main__":
    unittest.main()
