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
        self.assertIn("pull_request:\n    types: [opened, reopened]", self.source)
        self.assertIn("issue_comment:\n    types: [created, edited]", self.source)
        self.assertIn("workflow_dispatch:", self.source)
        self.assertNotIn("types: [opened, reopened, edited]", self.source)

    def test_preflight_rejects_bots_and_unsupported_events_before_login(self):
        preflight = self.source.index("- name: Validate issue event")
        azure_login = self.source.index("- name: Azure login")
        self.assertLess(preflight, azure_login)
        self.assertIn('"$actor_type" != "User"', self.source)
        self.assertIn('"$comment_author_type" != "User"', self.source)
        self.assertIn("reason=bot_activity", self.source)
        self.assertIn("reason=bot_comment", self.source)
        self.assertIn("reason=invalid_comment_id", self.source)
        self.assertIn("reason=comment_actor_mismatch", self.source)
        self.assertIn("reason=unsupported_pull_request_action", self.source)
        self.assertEqual(
            self.source.count("if: steps.preflight.outputs.eligible == 'true'"),
            2,
        )

    def test_concurrency_remains_per_issue(self):
        self.assertIn(
            "issuelens-triage-${{ github.repository }}-${{ "
            "github.event.issue.number || github.event.pull_request.number || "
            "inputs.issue_number || github.run_id }}",
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
            "subject_type",
            "actor_login",
            "actor_type",
            "issue_author_association",
            "comment_id",
            "comment_author_login",
            "comment_author_association",
            "comment_author_type",
            "comment_added",
            "comment_edited",
            "manual_dispatch",
        ):
            self.assertIn(field, self.source)
        self.assertNotIn(".comment.body", self.source)
        self.assertNotIn(".issue.body", self.source)

    def test_optional_metadata_uses_null_when_unknown(self):
        self.assertIn(
            'issue_author_association: (if $issue_author_association == "" '
            'then null else $issue_author_association end)',
            self.source,
        )
        self.assertIn(
            'comment_author_association: (if $comment_author_association == "" '
            'then null else $comment_author_association end)',
            self.source,
        )
        self.assertIn(
            'comment_author_login: (if $comment_author_login == "" '
            'then null else $comment_author_login end)',
            self.source,
        )
        self.assertIn(
            'comment_id: (if $comment_id == "" then null else '
            '($comment_id | tonumber) end)',
            self.source,
        )

    def test_invocation_is_neutral_and_supports_no_action(self):
        self.assertIn("trusted IssueLens issue-loop event", self.source)
        self.assertIn("global built-in command and trusted issue-loop contracts", self.source)
        self.assertIn("Trusted event metadata: ${EVENT_METADATA}", self.source)
        self.assertNotIn("@issuelens ", self.source)
        self.assertNotIn("initial triage, re-triage", self.source)
        self.assertNotIn("responsibility-first rules", self.source)
        self.assertNotIn("this workflow authorizes", self.source)
        self.assertNotIn("validated planning policy", self.source)
        self.assertNotIn("privileged authorization", self.source)
        self.assertNotIn('input="Triage GitHub issue', self.source)

    def test_documentation_describes_event_loop_boundaries(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("issue-comment created/edited", readme)
        self.assertIn("pull request opened/reopened", readme)
        self.assertIn(
            "triage, re-triage, planning, re-planning, or no action",
            readme,
        )
        self.assertIn("does not currently trigger on issue title/body edits", readme)
        self.assertIn("reacts with 👀", readme)
        self.assertIn("bursts may coalesce", readme)
        self.assertIn("### Built-in commands", readme)
        self.assertIn("`@issuelens go` is not planning approval", readme)
        self.assertIn("workflow carries that provenance but does not\nparse", readme)
        self.assertIn("commands inside Markdown block quotes", readme)
        self.assertIn("inline code, fenced code blocks, or\npasted logs", readme)
        self.assertIn("no-action decision performs no GitHub write", readme)


if __name__ == "__main__":
    unittest.main()
