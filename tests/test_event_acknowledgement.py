import json
import unittest

from event_acknowledgement import ReactionTracker, reaction_target


def trusted_prompt(**overrides):
    metadata = {
        "event_name": "issues",
        "event_action": "opened",
        "repository": "microsoft/IssueLens",
        "issue_number": 21,
        "subject_type": "issue",
        "actor_login": "maintainer",
        "actor_type": "User",
        "comment_id": None,
        "comment_author_login": None,
        "comment_author_type": None,
    }
    metadata.update(overrides)
    return (
        "Process the trusted IssueLens issue-loop event for "
        f"{metadata['repository']}#{metadata['issue_number']} under the global "
        "built-in command and trusted issue-loop contracts. Trusted event "
        f"metadata: {json.dumps(metadata)}"
    )


class RecordingClient:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def add_eyes_reaction(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return {"content": "eyes"}


class ReactionTargetTests(unittest.TestCase):
    def test_issue_and_pull_request_bodies_are_supported(self):
        issue = reaction_target(trusted_prompt())
        pull_request = reaction_target(trusted_prompt(
            event_name="pull_request",
            event_action="reopened",
            subject_type="pull_request",
        ))

        self.assertEqual(
            (issue.repository, issue.subject_type, issue.subject_number, issue.comment_id),
            ("microsoft/IssueLens", "issue", 21, None),
        )
        self.assertEqual(
            (
                pull_request.repository,
                pull_request.subject_type,
                pull_request.subject_number,
                pull_request.comment_id,
            ),
            ("microsoft/IssueLens", "pull_request", 21, None),
        )

    def test_issue_and_pull_request_comments_use_the_triggering_comment(self):
        for subject_type in ("issue", "pull_request"):
            with self.subTest(subject_type=subject_type):
                target = reaction_target(trusted_prompt(
                    event_name="issue_comment",
                    event_action="created",
                    subject_type=subject_type,
                    comment_id=987,
                    comment_author_login="maintainer",
                    comment_author_type="User",
                ))

                self.assertEqual(target.comment_id, 987)
                self.assertEqual(target.subject_type, subject_type)

    def test_rejected_and_unsupported_events_have_no_target(self):
        prompts = (
            trusted_prompt(actor_type="Bot"),
            trusted_prompt(
                event_name="issue_comment",
                event_action="created",
                comment_id=987,
                comment_author_login="maintainer",
                comment_author_type="Bot",
            ),
            trusted_prompt(
                event_name="issue_comment",
                event_action="created",
                comment_id=987,
                comment_author_login="another-user",
                comment_author_type="User",
            ),
            trusted_prompt(event_action="closed"),
            trusted_prompt(event_name="workflow_dispatch"),
            trusted_prompt(event_name="pull_request", subject_type="issue"),
            trusted_prompt().replace(
                '"repository": "microsoft/IssueLens"',
                '"repository": "other/repository"',
            ),
            "Triage microsoft/IssueLens#21",
        )

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(reaction_target(prompt))


class ReactionTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_do_not_repeat_successful_reaction_requests(self):
        client = RecordingClient()
        tracker = ReactionTracker()
        target = reaction_target(trusted_prompt())

        self.assertTrue(await tracker.add(client, target))
        self.assertFalse(await tracker.add(client, target))

        self.assertEqual(client.calls, [
            ("microsoft/IssueLens", "issue", 21, None),
        ])

    async def test_failed_reaction_can_be_retried(self):
        tracker = ReactionTracker()
        target = reaction_target(trusted_prompt())
        failing = RecordingClient(RuntimeError("reaction unavailable"))

        with self.assertRaisesRegex(RuntimeError, "reaction unavailable"):
            await tracker.add(failing, target)

        succeeding = RecordingClient()
        self.assertTrue(await tracker.add(succeeding, target))
        self.assertEqual(len(failing.calls), 1)
        self.assertEqual(len(succeeding.calls), 1)


if __name__ == "__main__":
    unittest.main()
