import pathlib
import unittest

import yaml
import test_team_memory_workflow as action_tests


ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "issue-triage.yml"


class IssueTriageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.source, Loader=yaml.BaseLoader)
        cls.job = cls.workflow["jobs"]["orchestrate"]

    def test_supported_events_are_explicit(self):
        self.assertIn("types: [opened, reopened]", self.source)
        self.assertIn("issue_comment:\n    types: [created, edited]", self.source)
        self.assertIn("workflow_dispatch:", self.source)
        self.assertNotIn("types: [opened, reopened, edited]", self.source)

    def test_preflight_rejects_pr_and_bot_comments_before_login(self):
        gate = self.job["if"]
        self.assertIn("github.event.issue.pull_request == null", gate)
        self.assertIn("github.event.sender.type == 'User'", gate)
        self.assertIn("github.event.comment.user.type == 'User'", gate)
        self.assertIn("github.event.repository.default_branch", gate)
        metadata = yaml.load((action_tests.ACTION_DIR / "action.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        preflight, login, submit = metadata["runs"]["steps"]
        self.assertEqual(preflight["id"], "preflight")
        for step in (login, submit):
            self.assertEqual(step["if"], "steps.preflight.outputs.eligible == 'true'")

    def test_caller_is_thin_and_loads_trusted_action(self):
        checkout, invoke = self.job["steps"]
        self.assertEqual(checkout["with"]["ref"], "${{ github.workflow_sha }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(checkout["with"]["sparse-checkout"], "/.github/actions/issuelens/")
        self.assertEqual(invoke["uses"], "./.github/actions/issuelens")
        self.assertEqual(invoke["with"]["request-type"], "issue-loop")
        self.assertEqual(invoke["with"]["issue-number"], "${{ inputs.issue_number }}")
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "read", "issues": "read", "id-token": "write"})
        self.assertEqual(self.job["timeout-minutes"], "20")
        self.assertTrue(all("run" not in step for step in self.job["steps"]))
        self.assertNotIn("pull_request.head", self.source)

    def test_concurrency_remains_per_issue(self):
        self.assertIn(
            "issuelens-triage-${{ github.repository }}-${{ "
            "github.event.issue.number || inputs.issue_number || github.run_id }}",
            self.source,
        )
        self.assertIn("cancel-in-progress: false", self.source)
        self.assertNotIn("issuelens-triage-${{ github.ref }}", self.source)

    def test_trusted_metadata_excludes_issue_and_comment_text(self):
        helper = action_tests.HELPER.read_text(encoding="utf-8")
        for field in (
            "event_name",
            "event_action",
            "repository",
            "issue_number",
            "actor_login",
            "actor_type",
            "issue_author_association",
            "comment_id",
            "comment_author_login",
            "comment_author_association",
            "comment_added",
            "comment_edited",
            "manual_dispatch",
        ):
            self.assertIn(field, helper)
        self.assertNotIn('.get("body")', helper)
        self.assertNotIn('["body"]', helper)

    def test_optional_metadata_uses_null_when_unknown(self):
        helper = action_tests.HELPER.read_text(encoding="utf-8")
        self.assertIn('"issue_author_association": issue.get("author_association") or None', helper)
        self.assertIn('"comment_author_association": comment.get("author_association") or None', helper)
        self.assertIn('"comment_author_login": comment.get("user", {}).get("login") or None', helper)
        self.assertIn('if event_name == "issue_comment" else None', helper)

    def test_invocation_is_neutral_and_supports_no_action(self):
        helper = action_tests.HELPER.read_text(encoding="utf-8")
        self.assertIn("trusted IssueLens issue-loop event", helper)
        self.assertIn("global built-in command and trusted issue-loop contracts", helper)
        self.assertIn("Trusted event metadata: ", helper)
        for text in ("@issuelens ", "initial triage, re-triage", "responsibility-first rules",
                     "this workflow authorizes", "validated planning policy", "privileged authorization"):
            self.assertNotIn(text, helper)

    def test_documentation_describes_event_loop_boundaries(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("issue-comment\n  created/edited", readme)
        self.assertIn("triage, re-triage, planning, re-planning, or\n  no action", readme)
        self.assertIn("does not currently trigger on issue title/body edits", readme)
        self.assertIn("rejects PR-backed comments", readme)
        self.assertIn("bursts may coalesce", readme)
        self.assertIn("### Built-in commands", readme)
        self.assertIn("`@issuelens go` is not planning approval", readme)
        self.assertIn("workflow carries that provenance but does not\nparse", readme)
        self.assertIn("commands inside Markdown block quotes", readme)
        self.assertIn("inline code, fenced code blocks, or\npasted logs", readme)
        self.assertIn("no-action decision performs no GitHub write", readme)


if __name__ == "__main__":
    unittest.main()
