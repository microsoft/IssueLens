import pathlib
import unittest

import yaml


ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "issue-triage.yml"


class IssueTriageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        yaml.compose(cls.source)

    def test_supported_events_are_explicit(self):
        self.assertIn("types: [opened, reopened]", self.source)
        self.assertIn("issue_comment:\n    types: [created, edited]", self.source)
        self.assertIn("workflow_dispatch:", self.source)
        self.assertNotIn("types: [opened, reopened, edited]", self.source)

    def test_preflight_rejects_pr_and_bot_comments_before_login(self):
        preflight = self.source.index("- name: Validate issue event")
        azure_login = self.source.index("- name: Azure login")
        self.assertLess(preflight, azure_login)
        self.assertIn(".issue.pull_request != null", self.source)
        self.assertIn('"$actor_type" != "User"', self.source)
        self.assertIn('"$comment_author_type" != "User"', self.source)
        self.assertIn("reason=pull_request_comment", self.source)
        self.assertIn("reason=bot_comment", self.source)
        self.assertEqual(
            self.source.count("if: steps.preflight.outputs.eligible == 'true'"),
            2,
        )

    def test_concurrency_remains_per_issue(self):
        self.assertIn(
            "issuelens-triage-${{ github.repository }}-${{ "
            "github.event.issue.number || inputs.issue_number || github.run_id }}",
            self.source,
        )
        self.assertIn("cancel-in-progress: false", self.source)
        self.assertNotIn("issuelens-triage-${{ github.ref }}", self.source)

    def test_trusted_metadata_excludes_issue_and_comment_text(self):
        for field in (
            "event_name",
            "event_action",
            "repository",
            "issue_number",
            "actor_login",
            "actor_type",
            "issue_author_association",
            "comment_id",
            "comment_author_association",
            "comment_added",
            "comment_edited",
            "manual_dispatch",
        ):
            self.assertIn(field, self.source)
        self.assertNotIn(".comment.body", self.source)
        self.assertNotIn(".issue.body", self.source)

    def test_invocation_is_neutral_and_supports_no_action(self):
        self.assertIn("initial triage, re-triage with new evidence", self.source)
        self.assertIn("initial planning, re-planning from feedback", self.source)
        self.assertIn("or no action", self.source)
        self.assertIn("responsibility-first rules", self.source)
        self.assertIn("perform no GitHub write", self.source)
        self.assertIn("as a privileged maintainer command", self.source)
        self.assertIn("this workflow authorizes appropriate existing labels", self.source)
        self.assertIn("assignment that preserves current assignees", self.source)
        self.assertIn("publication of planning artifacts", self.source)
        self.assertNotIn('input="Triage GitHub issue', self.source)

    def test_documentation_describes_event_loop_boundaries(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("issue-comment\n  created/edited", readme)
        self.assertIn("triage, re-triage, planning, re-planning, or\n  no action", readme)
        self.assertIn("does not currently trigger on issue title/body edits", readme)
        self.assertIn("rejects PR-backed comments", readme)
        self.assertIn("bursts may coalesce", readme)
        self.assertIn("no-action decision performs no GitHub write", readme)


if __name__ == "__main__":
    unittest.main()
