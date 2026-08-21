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
    ) -> int: ...

    async def remove_eyes_reaction(
        self,
        repository: str,
        subject_type: Literal["issue", "pull_request"],
        subject_number: int,
        reaction_id: int,
        comment_id: int | None = None,
    ) -> None: ...


@dataclass
class _TrackedReaction:
    add_task: asyncio.Task[int]
    users: int
    remove_task: asyncio.Task[None] | None = None


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
    """Share one start reaction and remove it after its final user finishes."""

    def __init__(self) -> None:
        self._reactions: dict[ReactionTarget, _TrackedReaction] = {}
        self._lock = asyncio.Lock()

    async def add(self, client: ReactionClient, target: ReactionTarget) -> bool:
        """Acquire the reaction, returning false when sharing an active request."""
        while True:
            async with self._lock:
                tracked = self._reactions.get(target)
                if tracked is None:
                    task = asyncio.create_task(client.add_eyes_reaction(
                        target.repository,
                        target.subject_type,
                        target.subject_number,
                        target.comment_id,
                    ))
                    tracked = _TrackedReaction(task, users=1)
                    self._reactions[target] = tracked
                    created = True
                    break
                if tracked.remove_task is None:
                    tracked.users += 1
                    task = tracked.add_task
                    created = False
                    break
                remove_task = tracked.remove_task

            try:
                await asyncio.shield(remove_task)
            except Exception:
                pass

        try:
            await asyncio.shield(task)
        except BaseException:
            async with self._lock:
                tracked = self._reactions.get(target)
                if tracked is not None and tracked.add_task is task:
                    tracked.users -= 1
                    if tracked.users == 0:
                        if task.done():
                            self._reactions.pop(target)
                        else:
                            tracked.remove_task = asyncio.create_task(
                                self._remove_after_add(client, target, tracked)
                            )
            raise
        return created

    async def remove(
        self,
        client: ReactionClient,
        target: ReactionTarget,
    ) -> bool:
        """Release the reaction and remove it after its final user finishes."""
        async with self._lock:
            tracked = self._reactions.get(target)
            if tracked is None or tracked.remove_task is not None:
                return False
            tracked.users -= 1
            if tracked.users > 0:
                return False
            reaction_id = tracked.add_task.result()
            tracked.remove_task = asyncio.create_task(
                self._remove_tracked(client, target, tracked, reaction_id)
            )
            remove_task = tracked.remove_task

        await asyncio.shield(remove_task)
        return True

    async def _remove_after_add(
        self,
        client: ReactionClient,
        target: ReactionTarget,
        tracked: _TrackedReaction,
    ) -> None:
        try:
            reaction_id = await tracked.add_task
        except BaseException:
            async with self._lock:
                if self._reactions.get(target) is tracked:
                    self._reactions.pop(target)
            return
        await self._remove_tracked(client, target, tracked, reaction_id)

    async def _remove_tracked(
        self,
        client: ReactionClient,
        target: ReactionTarget,
        tracked: _TrackedReaction,
        reaction_id: int,
    ) -> None:
        try:
            await client.remove_eyes_reaction(
                target.repository,
                target.subject_type,
                target.subject_number,
                reaction_id,
                target.comment_id,
            )
        finally:
            async with self._lock:
                if self._reactions.get(target) is tracked:
                    self._reactions.pop(target)
