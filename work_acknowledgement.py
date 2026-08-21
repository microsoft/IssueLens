"""Deterministic host safeguards for work acknowledgements."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import jwt


_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_GITHUB_OIDC_KEYS = jwt.PyJWKClient(
    f"{_GITHUB_OIDC_ISSUER}/.well-known/jwks"
)
_ISSUE_LOOP_AUDIENCE_PREFIX = "issuelens-issue-loop:"
_MAX_ENVELOPE_LENGTH = 16_384
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$"
)
_T = TypeVar("_T")


@dataclass(frozen=True)
class AcknowledgementTarget:
    """One host-validated activity that may receive an acknowledgement."""

    repository: str
    target_kind: Literal["issue", "issue_comment"]
    target_id: int


def trusted_issue_loop_target(metadata: Any) -> AcknowledgementTarget | None:
    """Select the triggering activity from valid trusted issue-loop metadata."""
    if not isinstance(metadata, dict):
        return None

    repository = metadata.get("repository")
    issue_number = metadata.get("issue_number")
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_PATTERN.fullmatch(repository)
        or not _positive_integer(issue_number)
        or metadata.get("actor_type") != "User"
    ):
        return None

    event_name = metadata.get("event_name")
    event_action = metadata.get("event_action")
    if event_name == "issues" and event_action in {"opened", "reopened"}:
        return AcknowledgementTarget(repository, "issue", issue_number)
    if (
        event_name == "workflow_dispatch"
        and event_action == "workflow_dispatch"
        and metadata.get("manual_dispatch") is True
    ):
        return AcknowledgementTarget(repository, "issue", issue_number)
    if event_name != "issue_comment" or event_action not in {"created", "edited"}:
        return None

    comment_id = metadata.get("comment_id")
    actor_login = metadata.get("actor_login")
    if (
        not _positive_integer(comment_id)
        or not isinstance(actor_login, str)
        or not actor_login
        or actor_login != metadata.get("comment_author_login")
    ):
        return None
    if event_action == "created":
        valid_action = (
            metadata.get("comment_added") is True
            and metadata.get("comment_edited") is False
        )
    else:
        valid_action = (
            metadata.get("comment_added") is False
            and metadata.get("comment_edited") is True
        )
    if not valid_action:
        return None
    return AcknowledgementTarget(repository, "issue_comment", comment_id)


def issue_loop_audience(metadata: dict[str, Any]) -> str:
    """Bind a GitHub OIDC token to one canonical event envelope."""
    serialized = json.dumps(
        metadata,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_ISSUE_LOOP_AUDIENCE_PREFIX}{hashlib.sha256(serialized).hexdigest()}"


def validated_issue_loop_envelope(
    encoded_envelope: str | None,
    token: str | None,
) -> dict[str, Any] | None:
    """Validate workflow provenance before creating a trusted host envelope."""
    if encoded_envelope is None and token is None:
        return None
    if (
        not encoded_envelope
        or not token
        or len(encoded_envelope) > _MAX_ENVELOPE_LENGTH
        or len(token) > _MAX_ENVELOPE_LENGTH
    ):
        raise ValueError("invalid issue-loop provenance")

    try:
        padding = "=" * (-len(encoded_envelope) % 4)
        raw_envelope = base64.b64decode(
            encoded_envelope + padding,
            altchars=b"-_",
            validate=True,
        )
        metadata = json.loads(raw_envelope.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid issue-loop provenance") from exc

    target = trusted_issue_loop_target(metadata)
    if target is None:
        raise ValueError("invalid issue-loop provenance")

    try:
        signing_key = _GITHUB_OIDC_KEYS.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=issue_loop_audience(metadata),
            issuer=_GITHUB_OIDC_ISSUER,
            options={"require": ["aud", "exp", "iat", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ValueError("invalid issue-loop provenance") from exc

    workflow_prefix = (
        f"{target.repository}/.github/workflows/issue-triage.yml@"
    )
    if (
        claims.get("repository") != target.repository
        or claims.get("event_name") != metadata.get("event_name")
        or claims.get("actor") != metadata.get("actor_login")
        or not str(claims.get("workflow_ref", "")).startswith(workflow_prefix)
    ):
        raise ValueError("invalid issue-loop provenance")
    return metadata


def trusted_issue_loop_prompt(metadata: dict[str, Any]) -> str:
    """Build the model prompt only from a host-validated event envelope."""
    target = trusted_issue_loop_target(metadata)
    if target is None:
        raise ValueError("invalid issue-loop provenance")
    return (
        "Process the trusted IssueLens issue-loop event for "
        f"{target.repository}#{metadata['issue_number']} under the global "
        "built-in command and trusted issue-loop contracts. Trusted event "
        f"metadata: {json.dumps(metadata, separators=(',', ':'), sort_keys=True)}"
    )


def acknowledgement_preflight(
    *,
    trusted_issue_loop_event: dict[str, Any] | None,
    has_explicit_issue_reference: bool,
) -> tuple[bool, AcknowledgementTarget | None]:
    """Decide whether the host must run the acknowledgement-only turn."""
    if trusted_issue_loop_event is not None:
        target = trusted_issue_loop_target(trusted_issue_loop_event)
        return target is not None, target
    return has_explicit_issue_reference, None


def acknowledgement_preflight_turn(
    turn: str,
    target: AcknowledgementTarget | None,
) -> str:
    """Restrict a model turn to acknowledgement eligibility and delivery."""
    if target is None:
        target_instruction = (
            "Identify the exact triggering issue, pull request, or comment from "
            "the authenticated current request."
        )
    else:
        target_instruction = (
            "The trusted host selected this triggering activity: "
            f'{{"repository":{json.dumps(target.repository)},'
            f'"target_kind":{json.dumps(target.target_kind)},'
            f'"target_id":{target.target_id}}}.'
        )
    return (
        f"{turn}\n\n"
        "Host phase: perform only the work-acknowledgement preflight. "
        f"{target_instruction} Use only the bounded reads needed to decide "
        "whether this is accepted, supported work rather than rejected, "
        "unsupported, unrelated, or a No action outcome. If it is accepted, "
        "call `add_eyes_reaction` for exactly that activity. Do not route, "
        "analyze, notify, or make any other write yet. If the reaction fails, "
        "retain that failure for the continuation and do not stop the task. "
        "End this turn after the preflight."
    )


def acknowledgement_continuation_turn(turn: str) -> str:
    """Resume the original request after the hidden acknowledgement preflight."""
    return (
        f"{turn}\n\n"
        "Host phase: the work-acknowledgement preflight is complete. Continue "
        "the original request now. Do not add another acknowledgement unless "
        "retrying the same bounded target after a failed call, and report any "
        "reaction failure honestly."
    )


async def load_after_acknowledgement(
    preflight: Callable[[], Awaitable[None]] | None,
    load: Callable[[], Awaitable[_T]],
) -> tuple[_T, Exception | None]:
    """Run acknowledgement preflight before loading issue-body images."""
    error = None
    if preflight is not None:
        try:
            await preflight()
        except Exception as exc:  # The main task must continue after failure.
            error = exc
    return await load(), error


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
