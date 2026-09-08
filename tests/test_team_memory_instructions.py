import ast
import json
import pathlib
import unittest

import yaml
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
        self.assertIn("stop memory maintenance", prompt)
        self.assertIn("priority knowledge areas", prompt)
        self.assertIn("Wiki writes belong to this maintenance job", prompt)
        self.assertIn("publication result confirms the wiki commit", prompt)
        self.assertIn("wiki was not updated", prompt)

    def test_reader_is_discoverable_and_read_only(self):
        prompt = (ROOT / "skills" / "team-memory" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        frontmatter = yaml.safe_load(prompt.split("---", 2)[1])
        self.assertEqual(frontmatter["name"], "team-memory")
        self.assertIn("Read-only", frontmatter["description"])
        self.assertIn('domain="team_memory"', prompt)
        self.assertIn("stops wiki retrieval", prompt)
        self.assertIn("returned `content`", prompt)
        self.assertIn("must not create proposals", prompt)
        self.assertIn("same snapshot", prompt)
        self.assertIn("Do not delegate ordinary retrieval", prompt)

    def test_config_skill_documents_memory_domain_and_boundaries(self):
        prompt = (ROOT / "skills" / "issuelens-config" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- `team_memory`", prompt)
        self.assertIn("inclusion/exclusion guidance", prompt)
        self.assertIn("independent wiki-write authorization", prompt)

    def test_orchestrator_routes_maintenance_not_shared_retrieval(self):
        prompt = (ROOT / "agents.md").read_text(encoding="utf-8")
        self.assertIn("planning, and team-memory capabilities", prompt)
        self.assertIn("do not dispatch routine\nretrieval", prompt)
        self.assertIn("Retrieval cannot propose or publish wiki", prompt)
        self.assertIn("host publication capability is unavailable", prompt)


if __name__ == "__main__":
    unittest.main()
