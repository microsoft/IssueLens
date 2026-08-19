"""Validate trusted issue-loop events and deduplicate start reactions."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol


_TRUSTED_EVENT = re.compile(
    r"^Process the trusted IssueLens issue-loop event for "
    r"(?P<repository>[A-Za-z0-9][A-Za-z0-9-]{0,38}/"
    r"[A-Za-z0-9_.-]{1,100})#(?P<number>[1-9][0-9]*) under the global "
    r"built-in command and trusted issue-loop contracts\. Trusted event "
    r"metadata: (?P<metadata>\{.*\})$",
    re.DOTALL,
)
_SUPPORTED_ACTIONS = {"opened", "reopened"}
_SUPPORTED_COMMENT_ACTIONS = {"created", "edited"}
_MAX_COMPLETED_REACTIONS = 2_000


@dataclass(frozen=True)
class ReactionTarget:
    repository: str
    subject_type: Literal["issue", "pull_request"]
    subject_number: int
    comment_id: int | None = None


class ReactionClient(Protocol):
    async def add_eyes_reaction(
        self,
        repository: str,
        subject_type: Literal["issue", "pull_request"],
        subject_number: int,
        comment_id: int | None = None,
    ) -> Any: ...


def reaction_target(prompt: str) -> ReactionTarget | None:
    """Return a reaction target only for a validated trusted event prompt."""
    match = _TRUSTED_EVENT.fullmatch(prompt)
    if match is None:
        return None
    try:
        metadata = json.loads(match.group("metadata"))
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None

    repository = metadata.get("repository")
    subject_number = metadata.get("issue_number")
    subject_type = metadata.get("subject_type")
    event_name = metadata.get("event_name")
    event_action = metadata.get("event_action")
    if (
        repository != match.group("repository")
        or type(subject_number) is not int
        or subject_number != int(match.group("number"))
        or subject_type not in {"issue", "pull_request"}
        or metadata.get("actor_type") != "User"
    ):
        return None

    if event_name in {"issues", "pull_request"}:
        expected_type = "issue" if event_name == "issues" else "pull_request"
        if event_action not in _SUPPORTED_ACTIONS or subject_type != expected_type:
            return None
        comment_id = None
    elif event_name == "issue_comment":
        comment_id = metadata.get("comment_id")
        if (
            event_action not in _SUPPORTED_COMMENT_ACTIONS
            or metadata.get("comment_author_type") != "User"
            or metadata.get("comment_author_login")
            != metadata.get("actor_login")
            or type(comment_id) is not int
            or comment_id < 1
        ):
            return None
    else:
        return None

    return ReactionTarget(
        repository=repository,
        subject_type=subject_type,
        subject_number=subject_number,
        comment_id=comment_id,
    )


class ReactionTracker:
    """Suppress repeated successful requests while allowing failed retries."""

    def __init__(self) -> None:
        self._completed: dict[ReactionTarget, None] = {}
        self._in_flight: dict[ReactionTarget, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    async def add(self, client: ReactionClient, target: ReactionTarget) -> bool:
        """Add the fixed reaction, returning false when already completed."""
        async with self._lock:
            if target in self._completed:
                return False
            task = self._in_flight.get(target)
            created = task is None
            if task is None:
                task = asyncio.create_task(client.add_eyes_reaction(
                    target.repository,
                    target.subject_type,
                    target.subject_number,
                    target.comment_id,
                ))
                self._in_flight[target] = task

        try:
            await task
        except BaseException:
            if created:
                async with self._lock:
                    self._in_flight.pop(target, None)
            raise

        if created:
            async with self._lock:
                self._in_flight.pop(target, None)
                self._completed[target] = None
                if len(self._completed) > _MAX_COMPLETED_REACTIONS:
                    self._completed.pop(next(iter(self._completed)))
        return created
