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
        self.assertIn("You are IssueLens", global_prompt)
        self.assertIn("current scope is issue triage", global_prompt)
        self.assertIn("main orchestrator", global_prompt)
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
        self.assertIn('"assign-issue"', main_source)
        self.assertIn('"notify"', main_source)
        self.assertIn("issue_image_attachments", main_source)
        self.assertIn("RequestTokenProvider", main_source)
        self.assertNotIn('"name": "issue-triage"', main_source)
        self.assertNotIn("critical-issue-analyst", main_source)


if __name__ == "__main__":
    unittest.main()
