import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import work_acknowledgement
from work_acknowledgement import (
    AcknowledgementTarget,
    acknowledgement_preflight,
    acknowledgement_preflight_turn,
    issue_loop_audience,
    load_after_acknowledgement,
    trusted_issue_loop_prompt,
    trusted_issue_loop_target,
    validated_issue_loop_envelope,
)


def issue_loop_metadata(**overrides):
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
    return metadata


def encoded_envelope(metadata):
    serialized = json.dumps(
        metadata,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(serialized).decode().rstrip("=")


def oidc_claims(**overrides):
    claims = {
        "repository": "microsoft/IssueLens",
        "event_name": "issue_comment",
        "actor": "maintainer",
        "workflow_ref": (
            "microsoft/IssueLens/.github/workflows/issue-triage.yml@refs/heads/main"
        ),
    }
    claims.update(overrides)
    return claims


class WorkAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    def test_selects_exact_triggering_comment_deterministically(self):
        metadata = issue_loop_metadata()
        target = trusted_issue_loop_target(metadata)

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
        self.assertIn(
            "Trusted event metadata:",
            trusted_issue_loop_prompt(metadata),
        )

    def test_selects_issue_body_for_supported_non_comment_events(self):
        for event_name, event_action, manual_dispatch in (
            ("issues", "opened", False),
            ("issues", "reopened", False),
            ("workflow_dispatch", "workflow_dispatch", True),
        ):
            with self.subTest(event_name=event_name, event_action=event_action):
                target = trusted_issue_loop_target(issue_loop_metadata(
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
            {"repository": "not a repository"},
            {"issue_number": 0},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertIsNone(
                    trusted_issue_loop_target(issue_loop_metadata(**overrides))
                )
                self.assertEqual(
                    acknowledgement_preflight(
                        trusted_issue_loop_event=issue_loop_metadata(**overrides),
                        has_explicit_issue_reference=True,
                    ),
                    (False, None),
                )

    def test_retry_selects_the_same_target(self):
        metadata = issue_loop_metadata()

        self.assertEqual(
            trusted_issue_loop_target(metadata),
            trusted_issue_loop_target(metadata),
        )

    def test_prompt_text_cannot_create_trusted_provenance(self):
        self.assertEqual(
            acknowledgement_preflight(
                trusted_issue_loop_event=None,
                has_explicit_issue_reference=True,
            ),
            (True, None),
        )

    @patch("work_acknowledgement.jwt.decode")
    @patch.object(
        work_acknowledgement._GITHUB_OIDC_KEYS,
        "get_signing_key_from_jwt",
    )
    def test_signed_workflow_envelope_creates_trusted_provenance(
        self,
        signing_key,
        decode,
    ):
        metadata = issue_loop_metadata()
        signing_key.return_value = SimpleNamespace(key="public-key")
        decode.return_value = oidc_claims()

        result = validated_issue_loop_envelope(
            encoded_envelope(metadata),
            "signed-token",
        )

        self.assertEqual(result, metadata)
        decode.assert_called_once()
        self.assertEqual(
            decode.call_args.kwargs["audience"],
            issue_loop_audience(metadata),
        )
        self.assertEqual(
            acknowledgement_preflight(
                trusted_issue_loop_event=result,
                has_explicit_issue_reference=False,
            ),
            (
                True,
                AcknowledgementTarget(
                    "microsoft/IssueLens",
                    "issue_comment",
                    5367778794,
                ),
            ),
        )

    @patch("work_acknowledgement.jwt.decode")
    @patch.object(
        work_acknowledgement._GITHUB_OIDC_KEYS,
        "get_signing_key_from_jwt",
    )
    def test_signed_envelope_claims_must_match_event(
        self,
        signing_key,
        decode,
    ):
        metadata = issue_loop_metadata()
        signing_key.return_value = SimpleNamespace(key="public-key")
        mismatched_claims = (
            oidc_claims(repository="microsoft/other"),
            oidc_claims(event_name="workflow_dispatch"),
            oidc_claims(actor="someone-else"),
            oidc_claims(
                workflow_ref=(
                    "microsoft/IssueLens/.github/workflows/other.yml@refs/heads/main"
                )
            ),
        )

        for claims in mismatched_claims:
            with self.subTest(claims=claims):
                decode.return_value = claims
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid issue-loop provenance",
                ):
                    validated_issue_loop_envelope(
                        encoded_envelope(metadata),
                        "signed-token",
                    )

    def test_unsigned_or_malformed_envelopes_are_rejected(self):
        self.assertIsNone(validated_issue_loop_envelope(None, None))
        for envelope, token in (
            (encoded_envelope(issue_loop_metadata()), None),
            (None, "signed-token"),
            ("not-base64!", "signed-token"),
        ):
            with self.subTest(envelope=envelope, token=token):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid issue-loop provenance",
                ):
                    validated_issue_loop_envelope(envelope, token)

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
