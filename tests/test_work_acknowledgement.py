import json
import unittest

from work_acknowledgement import (
    AcknowledgementTarget,
    acknowledgement_preflight,
    acknowledgement_preflight_turn,
    load_after_acknowledgement,
    trusted_issue_loop_target,
)


def issue_loop_prompt(**overrides):
    metadata = {
        "event_name": "issue_comment",
        "event_action": "created",
        "repository": "microsoft/IssueLens",
        "issue_number": 24,
        "actor_login": "maintainer",
        "actor_type": "User",
        "comment_id": 5367778794,
        "comment_author_login": "maintainer",
        "comment_added": True,
        "comment_edited": False,
        "manual_dispatch": False,
    }
    metadata.update(overrides)
    return (
        "Process the trusted IssueLens issue-loop event for "
        "microsoft/IssueLens#24 under the global built-in command and trusted "
        "issue-loop contracts. Trusted event metadata: "
        f"{json.dumps(metadata)}"
    )


class WorkAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    def test_selects_exact_triggering_comment_deterministically(self):
        target = trusted_issue_loop_target(issue_loop_prompt())

        self.assertEqual(
            target,
            AcknowledgementTarget(
                "microsoft/IssueLens",
                "issue_comment",
                5367778794,
            ),
        )
        turn = acknowledgement_preflight_turn("request", target)
        self.assertIn('"target_kind":"issue_comment"', turn)
        self.assertIn('"target_id":5367778794', turn)

    def test_selects_issue_body_for_supported_non_comment_events(self):
        for event_name, event_action, manual_dispatch in (
            ("issues", "opened", False),
            ("issues", "reopened", False),
            ("workflow_dispatch", "workflow_dispatch", True),
        ):
            with self.subTest(event_name=event_name, event_action=event_action):
                target = trusted_issue_loop_target(issue_loop_prompt(
                    event_name=event_name,
                    event_action=event_action,
                    manual_dispatch=manual_dispatch,
                ))
                self.assertEqual(
                    target,
                    AcknowledgementTarget(
                        "microsoft/IssueLens",
                        "issue",
                        24,
                    ),
                )

    def test_rejected_or_unsupported_events_have_no_target(self):
        cases = (
            {"actor_type": "Bot"},
            {"event_name": "pull_request", "event_action": "opened"},
            {"event_action": "deleted"},
            {"comment_author_login": "someone-else"},
            {"comment_added": False},
            {"comment_id": 0},
            {"repository": "microsoft/other"},
            {"issue_number": 25},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertIsNone(
                    trusted_issue_loop_target(issue_loop_prompt(**overrides))
                )
                self.assertEqual(
                    acknowledgement_preflight(
                        issue_loop_prompt(**overrides),
                        has_explicit_issue_reference=True,
                    ),
                    (False, None),
                )

    def test_retry_selects_the_same_target(self):
        prompt = issue_loop_prompt()

        self.assertEqual(
            trusted_issue_loop_target(prompt),
            trusted_issue_loop_target(prompt),
        )

    def test_explicit_responses_target_runs_untrusted_target_preflight(self):
        self.assertEqual(
            acknowledgement_preflight(
                "@issuelens triage microsoft/IssueLens#24",
                has_explicit_issue_reference=True,
            ),
            (True, None),
        )

    async def test_preflight_completes_before_image_loading(self):
        calls = []

        async def preflight():
            calls.append("acknowledge")

        async def load():
            calls.append("load-images")
            return ["image"]

        result, error = await load_after_acknowledgement(preflight, load)

        self.assertEqual(calls, ["acknowledge", "load-images"])
        self.assertEqual(result, ["image"])
        self.assertIsNone(error)

    async def test_preflight_failure_does_not_block_image_loading(self):
        calls = []

        async def preflight():
            calls.append("acknowledge")
            raise RuntimeError("reaction failed")

        async def load():
            calls.append("load-images")
            return []

        result, error = await load_after_acknowledgement(preflight, load)

        self.assertEqual(calls, ["acknowledge", "load-images"])
        self.assertEqual(result, [])
        self.assertIsInstance(error, RuntimeError)


if __name__ == "__main__":
    unittest.main()
