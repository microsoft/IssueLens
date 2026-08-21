"""Deterministic host safeguards for work acknowledgements."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar


_TRUSTED_ISSUE_LOOP_PREFIX = "Process the trusted IssueLens issue-loop event for "
_TRUSTED_ISSUE_LOOP_PATTERN = re.compile(
    r"^Process the trusted IssueLens issue-loop event for "
    r"([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100})#([1-9][0-9]*) "
    r"under the global built-in command and trusted issue-loop contracts\. "
    r"Trusted event metadata: "
)
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


def is_trusted_issue_loop_prompt(prompt: str) -> bool:
    """Whether a prompt claims the workflow-owned issue-loop envelope."""
    return prompt.startswith(_TRUSTED_ISSUE_LOOP_PREFIX)


def trusted_issue_loop_target(prompt: str) -> AcknowledgementTarget | None:
    """Select the triggering activity from valid trusted issue-loop metadata."""
    envelope = _TRUSTED_ISSUE_LOOP_PATTERN.match(prompt)
    if envelope is None:
        return None
    try:
        metadata = json.loads(prompt[envelope.end():])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(metadata, dict):
        return None

    repository = metadata.get("repository")
    issue_number = metadata.get("issue_number")
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_PATTERN.fullmatch(repository)
        or not _positive_integer(issue_number)
        or repository != envelope.group(1)
        or issue_number != int(envelope.group(2))
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


def acknowledgement_preflight(
    prompt: str,
    *,
    has_explicit_issue_reference: bool,
) -> tuple[bool, AcknowledgementTarget | None]:
    """Decide whether the host must run the acknowledgement-only turn."""
    target = trusted_issue_loop_target(prompt)
    if is_trusted_issue_loop_prompt(prompt):
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
