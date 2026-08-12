import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class AgentPromptTests(unittest.TestCase):
    def test_prompt_files_and_wiring(self):
        global_prompt = (ROOT / "agents.md").read_text(encoding="utf-8")
        triage_prompt = (ROOT / "agents" / "triage.md").read_text(
            encoding="utf-8"
        )
        criticals_prompt = (ROOT / "agents" / "find-criticals.md").read_text(
            encoding="utf-8"
        )
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")

        ast.parse(main_source)
        self.assertIn("IssueLens orchestrator", global_prompt)
        self.assertIn("Do not perform the delegated", global_prompt)
        self.assertIn("Do not duplicate a sub-agent's analysis", global_prompt)
        self.assertIn("`triage` sub-agent", global_prompt)
        self.assertIn("`find-criticals` sub-agent", global_prompt)
        self.assertIn("task-appropriate response", triage_prompt)
        self.assertIn("host preloads", triage_prompt)
        self.assertIn("untrusted issue content", triage_prompt)
        self.assertNotIn("Return only one valid JSON object", triage_prompt)
        self.assertIn("Return only the final JSON object", criticals_prompt)
        self.assertIn('_project_dir / "agents.md"', main_source)
        self.assertIn('_agents_dir / "triage.md"', main_source)
        self.assertIn('_agents_dir / "find-criticals.md"', main_source)
        self.assertIn('"agent": "issuelens"', main_source)
        self.assertIn('"name": "triage"', main_source)
        self.assertIn('"name": "find-criticals"', main_source)
        self.assertIn('"find-duplicates"', main_source)
        self.assertIn('"issuelens-config"', main_source)
        self.assertIn('"label-issue"', main_source)
        duplicate_skill = (
            ROOT / "skills" / "find-duplicates" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("configured related repository", duplicate_skill)
        self.assertIn("may be read anonymously", duplicate_skill)
        self.assertIn("Comment count and timing follow the user's request", triage_prompt)
        self.assertIn("support engineer", triage_prompt)
        self.assertIn("explicitly asks for multiple comments", triage_prompt)
        self.assertIn("inaccessible repositories", triage_prompt)
        self.assertIn('"assign-issue"', main_source)
        self.assertIn('"notify"', main_source)
        self.assertIn("issue_image_attachments", main_source)
        self.assertIn("GitHubAppTokenProvider", main_source)
        self.assertIn('"type": "stdio"', main_source)
        self.assertIn('"GITHUB_MCP_ENABLE_WRITES": "true"', main_source)
        self.assertIn("_new_host_github_client", main_source)
        self.assertNotIn("RequestTokenProvider", main_source)
        self.assertNotIn("github-access", main_source)
        self.assertNotIn("issuelens-related-read", main_source)
        self.assertNotIn('data.get("github_token")', main_source)
        self.assertNotIn("api.githubcopilot.com/mcp", main_source)
        self.assertNotIn("_GITHUB_APP_PROVIDER", main_source)
        self.assertNotIn("_GITHUB_APP_CLIENT", main_source)
        self.assertNotIn('"name": "issue-triage"', main_source)
        self.assertNotIn("critical-issue-analyst", main_source)


if __name__ == "__main__":
    unittest.main()
