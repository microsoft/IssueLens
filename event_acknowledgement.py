"""Validate trusted issue-loop events and deduplicate start reactions."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Literal, Protocol


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$"
)
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_ASSOCIATIONS = {
    None,
    "COLLABORATOR",
    "CONTRIBUTOR",
    "FIRST_TIMER",
    "FIRST_TIME_CONTRIBUTOR",
    "MANNEQUIN",
    "MEMBER",
    "NONE",
    "OWNER",
}
_SUPPORTED_ACTIONS = {"opened", "reopened"}
_SUPPORTED_COMMENT_ACTIONS = {"created", "edited"}
_MAX_COMPLETED_REACTIONS = 2_000
_EVENT_FIELDS = {
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
}


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


def validated_event_metadata(value: Any) -> dict[str, Any] | None:
    """Validate the workflow-owned event envelope without accepting content."""
    if not isinstance(value, Mapping) or set(value) != _EVENT_FIELDS:
        return None
    metadata = dict(value)

    repository = metadata.get("repository")
    subject_number = metadata.get("issue_number")
    subject_type = metadata.get("subject_type")
    event_name = metadata.get("event_name")
    event_action = metadata.get("event_action")
    if (
        not isinstance(repository, str)
        or _REPOSITORY.fullmatch(repository) is None
        or type(subject_number) is not int
        or subject_number < 1
        or subject_type not in {"issue", "pull_request"}
        or not isinstance(metadata.get("actor_login"), str)
        or _LOGIN.fullmatch(metadata["actor_login"]) is None
        or metadata.get("actor_type") != "User"
        or metadata.get("issue_author_association") not in _ASSOCIATIONS
        or metadata.get("comment_author_association") not in _ASSOCIATIONS
        or type(metadata.get("comment_added")) is not bool
        or type(metadata.get("comment_edited")) is not bool
        or type(metadata.get("manual_dispatch")) is not bool
    ):
        return None

    if event_name in {"issues", "pull_request"}:
        expected_type = "issue" if event_name == "issues" else "pull_request"
        if (
            event_action not in _SUPPORTED_ACTIONS
            or subject_type != expected_type
            or metadata.get("comment_id") is not None
            or metadata.get("comment_author_login") is not None
            or metadata.get("comment_author_type") is not None
            or metadata["comment_added"]
            or metadata["comment_edited"]
            or metadata["manual_dispatch"]
        ):
            return None
    elif event_name == "issue_comment":
        comment_id = metadata.get("comment_id")
        is_created = event_action == "created"
        if (
            event_action not in _SUPPORTED_COMMENT_ACTIONS
            or metadata.get("comment_author_type") != "User"
            or not isinstance(metadata.get("comment_author_login"), str)
            or _LOGIN.fullmatch(metadata["comment_author_login"]) is None
            or metadata.get("comment_author_login")
            != metadata.get("actor_login")
            or type(comment_id) is not int
            or comment_id < 1
            or metadata["comment_added"] is not is_created
            or metadata["comment_edited"] is is_created
            or metadata["manual_dispatch"]
        ):
            return None
    elif event_name == "workflow_dispatch":
        if (
            event_action != "workflow_dispatch"
            or subject_type != "issue"
            or metadata.get("comment_id") is not None
            or metadata.get("comment_author_login") is not None
            or metadata.get("comment_author_type") is not None
            or metadata["comment_added"]
            or metadata["comment_edited"]
            or not metadata["manual_dispatch"]
        ):
            return None
    else:
        return None
    return metadata


def reaction_target(metadata: Any) -> ReactionTarget | None:
    """Return a reaction target only for an eligible validated event."""
    metadata = validated_event_metadata(metadata)
    if metadata is None or metadata["event_name"] == "workflow_dispatch":
        return None

    return ReactionTarget(
        repository=metadata["repository"],
        subject_type=metadata["subject_type"],
        subject_number=metadata["issue_number"],
        comment_id=metadata["comment_id"],
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
