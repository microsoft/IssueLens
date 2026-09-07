"""Durable, review-first team-memory proposal records.

This module intentionally does not publish. Publication is a trusted host
operation and must use a separately authorized capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import PurePosixPath
from typing import Any, Mapping


class TeamMemoryError(RuntimeError):
    """Raised for invalid or unavailable team-memory state."""


def _safe_page(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise TeamMemoryError("wiki page path is invalid")
    parsed = PurePosixPath(path)
    if path.startswith("/") or "\\" in path or any(p in {"", ".", ".."} for p in parsed.parts):
        raise TeamMemoryError("wiki page path must be repository-relative POSIX")
    if not path.casefold().endswith(".md"):
        raise TeamMemoryError("generated wiki pages must be Markdown")
    return path


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class ProposalStore:
    """SQLite-backed proposal store; ephemeral in-memory state is forbidden."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("ISSUELENS_TEAM_MEMORY_STORE")
        if not self.path:
            raise TeamMemoryError(
                "ISSUELENS_TEAM_MEMORY_STORE must point to a durable shared database"
            )
        if self.path == ":memory:":
            raise TeamMemoryError("team-memory store cannot be in-memory")
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS team_memory_proposals (
              proposal_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
              source_revision TEXT NOT NULL, wiki_base TEXT NOT NULL,
              content_hash TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL, approval TEXT, created_at TEXT NOT NULL
            )"""
        )
        self._db.commit()

    def propose(
        self,
        repository: str,
        source_revision: str,
        wiki_base: str,
        pages: Mapping[str, str],
        evidence: list[Mapping[str, str]],
    ) -> dict[str, Any]:
        if not repository or not source_revision or not wiki_base:
            raise TeamMemoryError("repository, source_revision, and wiki_base are required")
        normalized = {_safe_page(k): v for k, v in pages.items()}
        if any(not isinstance(v, str) or len(v.encode()) > 256 * 1024 for v in normalized.values()):
            raise TeamMemoryError("wiki page content is invalid or too large")
        payload = {"pages": normalized, "evidence": list(evidence)}
        content_hash = _digest(payload)
        existing = self._db.execute(
            "SELECT proposal_id FROM team_memory_proposals WHERE repository=? AND content_hash=? AND status != 'published'",
            (repository, content_hash),
        ).fetchone()
        proposal_id = existing[0] if existing else str(uuid.uuid4())
        if not existing:
            self._db.execute(
                "INSERT INTO team_memory_proposals VALUES (?,?,?,?,?,?,?,NULL,datetime('now'))",
                (proposal_id, repository, source_revision, wiki_base, content_hash,
                 json.dumps(payload, ensure_ascii=False), "proposed"),
            )
            self._db.commit()
        return {
            "proposal_id": proposal_id,
            "repository": repository,
            "source_revision": source_revision,
            "wiki_base": wiki_base,
            "content_hash": content_hash,
            "pages": sorted(normalized),
            "evidence": list(evidence),
            "status": "proposed",
        }

    def approve(self, proposal_id: str, content_hash: str, source_revision: str, wiki_base: str) -> None:
        row = self._db.execute(
            "SELECT content_hash, source_revision, wiki_base, status FROM team_memory_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if not row or row[:3] != (content_hash, source_revision, wiki_base) or row[3] != "proposed":
            raise TeamMemoryError("proposal approval does not exactly match current proposal")
        self._db.execute(
            "UPDATE team_memory_proposals SET status='approved', approval=? WHERE proposal_id=?",
            (json.dumps({"content_hash": content_hash, "source_revision": source_revision, "wiki_base": wiki_base}), proposal_id),
        )
        self._db.commit()

    def get(self, proposal_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT proposal_id, repository, source_revision, wiki_base, content_hash, payload, status FROM team_memory_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if not row:
            raise TeamMemoryError("proposal not found")
        result = dict(zip(("proposal_id", "repository", "source_revision", "wiki_base", "content_hash", "payload", "status"), row))
        result["payload"] = json.loads(result["payload"])
        return result
