"""Immutable, repository-confined change inventories and small source pages.

Only Git tree/blob reads have a larger ingress allowance. Cursors are portable
continuation data with an integrity checksum, not credentials or authorization.
All pages re-establish repository access before consulting the bounded cache.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import hashlib
import json
import re
import sys
import time
import unicodedata
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

import httpx

from .auth import GitHubAppError, GitHubAppTokenProvider, InstallationCredential, validate_repository


CHANGE_FORMAT_VERSION = 1
MAX_CHANGE_BLOB_BYTES = 4 * 1024 * 1024
MAX_CHANGE_OBJECT_HTTP_BYTES = 8 * 1024 * 1024
MAX_CHANGE_OPERATION_BYTES = 64 * 1024 * 1024
MAX_CHANGE_OPERATION_REQUESTS = 1024
MAX_CHANGE_SESSION_BYTES = 512 * 1024 * 1024
MAX_CHANGE_SESSION_REQUESTS = 16_384
MAX_CHANGE_SECONDS = 60
MAX_CHANGE_TREE_ENTRIES = 100_000
MAX_CHANGE_TREES = 1024
MAX_CHANGE_FILES = 20_000
MAX_CHANGE_DEPTH = 64
MAX_CHANGE_GRAPH_COMMITS = 512
MAX_CHANGE_PARENTS = 64
MAX_CHANGE_CACHE_BYTES = 64 * 1024 * 1024
MAX_CHANGE_CACHE_ENTRIES = 512
CHANGE_CACHE_TTL_SECONDS = 300
MAX_CHANGE_RESULT_BYTES = 32_768
MIN_CHANGE_PAGE_BYTES = 1024
DEFAULT_CHANGE_PAGE_BYTES = 24_576
MAX_CHANGE_CURSOR_BYTES = 2048
MAX_FILE_RANGE_LINES = 10_000
MAX_DIFF_MATCH_LINES = 2000
MAX_DIFF_MATCH_CELLS = 250_000
MAX_DIFF_MATCH_INPUT_BYTES = 256 * 1024
MAX_DIFF_MATCH_WORK = 16 * 1024 * 1024

_API_ROOT = "https://api.github.com"
_API_VERSION = "2026-03-10"
_METADATA_HTTP_BYTES = 128 * 1024
_OID = re.compile(r"[0-9a-fA-F]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_OBJECT_ROUTE = re.compile(r"/git/(commits|trees|blobs)/[0-9a-f]{40}")
_PULL_ROUTE = re.compile(r"/pulls/[1-9][0-9]{0,9}")
_MODES = {"040000": "tree", "100644": "blob", "100755": "blob", "120000": "blob", "160000": "commit"}


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise GitHubAppError(f"{name} must be an integer from {lower} to {upper}")
    return value


def _oid(value: Any, name: str = "SHA") -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise GitHubAppError(f"{name} must be a full 40-character hexadecimal SHA")
    return value.lower()


def _repository(value: Any) -> str:
    if not isinstance(value, str):
        raise GitHubAppError("Repository must use the owner/repository format")
    repository = validate_repository(value)
    if repository.split("/")[1] in {".", ".."}:
        raise GitHubAppError("Repository must use the owner/repository format")
    return repository


def _path(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > 240 or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(unicodedata.category(char).startswith("C") for char in value)
        or len(value.split("/")) > MAX_CHANGE_DEPTH
    ):
        raise GitHubAppError("path must be a bounded repository-relative POSIX file path")
    return value


def _identity(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(
        [CHANGE_FORMAT_VERSION, *parts], ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")).hexdigest()


def snapshot_id(repository: str, base_sha: str | None, head_sha: str) -> str:
    """The same comparison has the same identity, independently of caller mode."""
    return _identity("snapshot", repository.casefold(), base_sha, head_sha)


def serialized_size(value: Any) -> int:
    # MCP's JSON text uses indent=2 and Unicode. ASCII escaping is also counted
    # so this bounds both the real MCP result and ordinary JSON consumers.
    return len(json.dumps(value, ensure_ascii=True, indent=2).encode("utf-8"))


def _encode_cursor(data: dict[str, Any]) -> str:
    raw = json.dumps(
        {"v": CHANGE_FORMAT_VERSION, **data}, sort_keys=True,
        ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(b"issuelens-change-cursor\0" + raw).hexdigest()[:32]
    return f"{encoded}.{checksum}"


def _decode_cursor(value: Any, kind: str, keys: set[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        not isinstance(value, str) or len(value) > MAX_CHANGE_CURSOR_BYTES
        or not re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{32}", value)
    ):
        raise GitHubAppError("Invalid change cursor")
    encoded, checksum = value.split(".")
    try:
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True,
        )
        data = json.loads(raw)
        if (
            not isinstance(data, dict) or set(data) != keys | {"v", "k"}
            or type(data["v"]) is not int or data["v"] != CHANGE_FORMAT_VERSION
            or data["k"] != kind
            or hashlib.sha256(b"issuelens-change-cursor\0" + raw).hexdigest()[:32] != checksum
            or _encode_cursor({key: val for key, val in data.items() if key != "v"}) != value
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise GitHubAppError("Invalid or obsolete change cursor") from None
    return data


def _page_cursor(value: Any, kind: str, request_id: str) -> dict[str, Any] | None:
    data = _decode_cursor(value, kind, {"q", "i", "p", "o"})
    if data is not None:
        if (
            data["q"] != request_id or not isinstance(data["i"], str)
            or not _DIGEST.fullmatch(data["i"])
        ):
            raise GitHubAppError("Change cursor does not match this repository, snapshot, or path")
        _integer(data["p"], "cursor part", 0, 1)
        _integer(data["o"], "cursor offset", 0, MAX_CHANGE_BLOB_BYTES)
    return data


def _weight(value: Any, seen: set[int] | None = None) -> int:
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(_weight(getattr(value, field.name), seen) for field in fields(value))
    if isinstance(value, dict):
        return size + sum(_weight(key, seen) + _weight(item, seen) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return size + sum(_weight(item, seen) for item in value)
    return size


class _Cache:
    def __init__(self) -> None:
        self.items: OrderedDict[tuple[Any, ...], tuple[float, int, Any]] = OrderedDict()
        self.bytes = 0

    def _prune(self) -> None:
        now = time.monotonic()
        for key, (created, size, _) in list(self.items.items()):
            if now - created >= CHANGE_CACHE_TTL_SECONDS:
                del self.items[key]
                self.bytes -= size

    def get(self, key: tuple[Any, ...]) -> Any:
        self._prune()
        entry = self.items.get(key)
        if entry is None:
            return None
        self.items.move_to_end(key)
        return entry[2]

    def put(self, key: tuple[Any, ...], value: Any) -> None:
        self._prune()
        old = self.items.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
        size = _weight((key, value)) + 512
        if size > MAX_CHANGE_CACHE_BYTES:
            return
        while self.items and (
            self.bytes + size > MAX_CHANGE_CACHE_BYTES
            or len(self.items) >= MAX_CHANGE_CACHE_ENTRIES
        ):
            _, (_, removed, _) = self.items.popitem(last=False)
            self.bytes -= removed
        self.items[key] = (time.monotonic(), size, value)
        self.bytes += size


@dataclass(frozen=True, slots=True)
class _Commit:
    sha: str
    tree: str
    parents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Entry:
    sha: str
    mode: str
    kind: str
    size: int | None


@dataclass(frozen=True, slots=True)
class _Source:
    text: str
    starts: array
    status: str = "text"
    reason: str | None = None
    byte_size: int = 0

    def line_at(self, offset: int) -> int:
        return bisect_right(self.starts, offset) if self.text else 0

    def lines(self) -> list[str]:
        ends = [*self.starts[1:], len(self.text)]
        return [self.text[start:end] for start, end in zip(self.starts, ends)]


def _source(text: str, byte_size: int | None = None) -> _Source:
    starts = array("I", [0] if text else [])
    offset = text.find("\n")
    while offset >= 0:
        if offset + 1 < len(text):
            starts.append(offset + 1)
        offset = text.find("\n", offset + 1)
    return _Source(text, starts, byte_size=len(text.encode("utf-8")) if byte_size is None else byte_size)


@dataclass(frozen=True, slots=True)
class _Part:
    content: str
    representation: str
    source: _Source | None = None
    ends: tuple[int, ...] = ()
    old_lines: tuple[int, ...] = ()
    new_lines: tuple[int, ...] = ()

    def ranges(self, start: int, end: int) -> tuple[int, int, int, int]:
        if start == end:
            return (0, 0, 0, 0)
        if self.source is not None:
            first, last = self.source.line_at(start), self.source.line_at(end - 1)
            return (first, last, 0, 0) if self.representation == "replacement-old" else (0, 0, first, last)
        first, last = bisect_right(self.ends, start), bisect_left(self.ends, end) + 1
        old = [line for line in self.old_lines[first:last] if line]
        new = [line for line in self.new_lines[first:last] if line]
        return (
            min(old, default=0), max(old, default=0),
            min(new, default=0), max(new, default=0),
        )


@dataclass(frozen=True, slots=True)
class _Diff:
    identity: str
    old_sha: str | None
    new_sha: str | None
    parts: tuple[_Part, ...]
    status: str = "text"
    reason: str | None = None


class _Session:
    def __init__(self, owner: ChangeReader, repository: str) -> None:
        self.owner = owner
        self.repository = repository
        self.client = httpx.AsyncClient(
            transport=owner.transport, timeout=30, follow_redirects=False,
            trust_env=False,
        )
        self.credentials: dict[str, InstallationCredential | None] = {}
        self.requests = 0
        self.bytes = 0
        self.tree_entries = 0
        self.trees: set[str] = set()
        self.started = time.monotonic()

    def check(self) -> None:
        if time.monotonic() - self.started >= MAX_CHANGE_SECONDS:
            raise GitHubAppError("Change read exceeded its cooperative time budget")

    async def credential(self, permission: str) -> InstallationCredential | None:
        if permission not in self.credentials:
            try:
                credential = await self.owner.token_provider.get_token(
                    self.repository, {permission: "read"},
                )
            except GitHubAppError:
                credential = None
            if credential is not None and (
                credential.repository.casefold() != self.repository.casefold()
                or credential.permissions != ((permission, "read"),)
                or credential.expires_at <= time.time()
            ):
                raise GitHubAppError("GitHub change-read credential scope or expiry mismatch")
            self.credentials[permission] = credential
        return self.credentials[permission]

    async def authorize(self) -> None:
        if await self.credential("contents") is None:
            # An installation failure must never grant access to private data
            # retained from an earlier page in this client.
            metadata = await self.get("")
            if (
                not isinstance(metadata, dict) or metadata.get("private") is not False
                or not isinstance(metadata.get("full_name"), str)
                or metadata["full_name"].casefold() != self.repository.casefold()
                or metadata.get("visibility", "public") != "public"
            ):
                raise GitHubAppError("Repository is not verified publicly readable")

    async def get(self, path: str) -> Any:
        object_match = _OBJECT_ROUTE.fullmatch(path)
        is_pull = _PULL_ROUTE.fullmatch(path) is not None
        if path != "" and object_match is None and not is_pull:
            raise GitHubAppError("Unsupported change-read route")
        permission = "pull_requests" if is_pull else "contents"
        limit = (
            MAX_CHANGE_OBJECT_HTTP_BYTES
            if object_match is not None and object_match[1] in {"trees", "blobs"}
            else _METADATA_HTTP_BYTES
        )
        self.check()
        if (
            self.requests >= MAX_CHANGE_OPERATION_REQUESTS
            or self.owner.requests >= MAX_CHANGE_SESSION_REQUESTS
        ):
            raise GitHubAppError("Change read request budget exhausted")
        credential = await self.credential(permission)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "IssueLens-GitHub-MCP/0.1",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential.token}"
        self.requests += 1
        self.owner.requests += 1
        try:
            async with self.client.stream(
                "GET", f"{_API_ROOT}/repos/{self.repository}{path}", headers=headers,
            ) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared is not None and (not declared.isdecimal() or int(declared) > limit):
                    raise GitHubAppError("Git object response exceeds its endpoint byte limit")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    self.check()
                    self.bytes += len(chunk)
                    self.owner.bytes += len(chunk)
                    if len(chunk) > limit - len(content):
                        raise GitHubAppError("Git object response exceeds its endpoint byte limit")
                    if (
                        self.bytes > MAX_CHANGE_OPERATION_BYTES
                        or self.owner.bytes > MAX_CHANGE_SESSION_BYTES
                    ):
                        raise GitHubAppError("Change read download byte budget exhausted")
                    content.extend(chunk)
            payload = json.loads(content)
        except httpx.HTTPStatusError as error:
            if credential is None:
                if error.response.status_code == 403 and error.response.headers.get("x-ratelimit-remaining") == "0":
                    raise GitHubAppError("GitHub anonymous public-read rate limit exceeded") from None
                raise GitHubAppError("Repository/object is not publicly readable and the IssueLens App cannot access it") from None
            raise GitHubAppError(f"GitHub API returned HTTP {error.response.status_code}") from None
        except (httpx.HTTPError, ValueError):
            raise GitHubAppError("GitHub change-object request failed") from None
        self.check()
        return payload


class ChangeReader:
    """One session-owned bounded cache; no persistent files or cached credentials."""

    def __init__(
        self, token_provider: GitHubAppTokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token_provider = token_provider
        self.transport = transport
        self.cache = _Cache()
        self.requests = 0
        self.bytes = 0
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def _session(self, repository: str) -> AsyncIterator[_Session]:
        started = time.monotonic()
        deadline = asyncio.timeout(MAX_CHANGE_SECONDS)
        try:
            async with deadline, self.lock:
                session = _Session(self, repository)
                session.started = started
                try:
                    await session.authorize()
                    yield session
                    session.check()
                finally:
                    await session.client.aclose()
        except TimeoutError:
            if not deadline.expired():
                raise
            raise GitHubAppError("Change read exceeded its cooperative time budget") from None

    def _key(self, session: _Session, *parts: Any) -> tuple[Any, ...]:
        return (session.repository.casefold(), *parts)

    async def _commit(self, session: _Session, sha: str) -> _Commit:
        key = self._key(session, "commit", sha)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        payload = await session.get(f"/git/commits/{sha}")
        if (
            not isinstance(payload, dict) or _oid(payload.get("sha")) != sha
            or not isinstance(payload.get("tree"), dict)
            or not isinstance(payload.get("parents"), list)
            or len(payload["parents"]) > MAX_CHANGE_PARENTS
        ):
            raise GitHubAppError("GitHub returned an invalid Git commit object")
        parents = tuple(_oid(item.get("sha") if isinstance(item, dict) else None) for item in payload["parents"])
        if sha in parents or len(set(parents)) != len(parents):
            raise GitHubAppError("GitHub returned invalid commit parents")
        commit = _Commit(sha, _oid(payload["tree"].get("sha")), parents)
        self.cache.put(key, commit)
        return commit

    async def _tree(self, session: _Session, sha: str) -> dict[str, _Entry]:
        key = self._key(session, "tree", sha)
        entries = self.cache.get(key)
        if entries is None:
            payload = await session.get(f"/git/trees/{sha}")
            if (
                not isinstance(payload, dict) or _oid(payload.get("sha")) != sha
                or not isinstance(payload.get("tree"), list)
                or type(payload.get("truncated")) is not bool
            ):
                raise GitHubAppError("GitHub returned an invalid Git tree")
            if payload["truncated"]:
                raise GitHubAppError("Git tree is truncated; change inventory cannot be complete")
            if len(payload["tree"]) > MAX_CHANGE_TREE_ENTRIES:
                raise GitHubAppError("Change tree entry budget exhausted")
            entries = {}
            for item in payload["tree"]:
                if not isinstance(item, dict):
                    raise GitHubAppError("GitHub returned an invalid tree entry")
                name = _path(item.get("path"))
                mode, kind = item.get("mode"), item.get("type")
                if "/" in name or name in entries or not isinstance(mode, str) or mode not in _MODES or _MODES[mode] != kind:
                    raise GitHubAppError("GitHub returned an invalid nonrecursive tree entry")
                size = item.get("size")
                if kind == "blob":
                    _integer(size, "Git blob size", 0, 2**63 - 1)
                entries[name] = _Entry(_oid(item.get("sha")), mode, kind, size)
            self.cache.put(key, entries)
        if sha not in session.trees:
            session.trees.add(sha)
            session.tree_entries += len(entries)
            if len(session.trees) > MAX_CHANGE_TREES or session.tree_entries > MAX_CHANGE_TREE_ENTRIES:
                raise GitHubAppError("Change tree traversal budget exhausted")
        session.check()
        return entries

    async def _merge_base(self, session: _Session, base: str, head: str) -> str:
        key = self._key(session, "merge-base", base, head)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        colors: dict[str, int] = {}
        graph: dict[str, tuple[str, ...]] = {}
        stale: set[str] = set()
        queue: deque[str] = deque()
        processed: dict[str, int] = {}

        def paint(sha: str, color: int, ancestor: bool = False) -> None:
            pending = [(sha, color, ancestor)]
            while pending:
                node, flag, older = pending.pop()
                if older:
                    stale.add(node)
                updated = colors.get(node, 0) | flag
                if updated == colors.get(node, 0):
                    continue
                colors[node] = updated
                if len(colors) > MAX_CHANGE_GRAPH_COMMITS:
                    raise GitHubAppError("Merge-base commit graph budget exhausted")
                queue.append(node)
                if updated == 3:
                    pending.extend((parent, 3, True) for parent in graph.get(node, ()))

        paint(base, 1)
        paint(head, 2)
        while queue:
            session.check()
            node = queue.popleft()
            color = colors[node]
            if processed.get(node) == color:
                continue
            processed[node] = color
            if color == 3:
                continue
            commit = await self._commit(session, node)
            graph[node] = commit.parents
            for parent in commit.parents:
                paint(parent, color)
        candidates = {sha for sha, color in colors.items() if color == 3} - stale
        # A common node can be reached by a short side branch before a newer
        # common node's longer path reaches it. Paint the remaining candidates'
        # ancestry rather than mistaking the first intersection for a merge base.
        if len(candidates) > 1:
            for candidate in sorted(candidates):
                if candidate in stale:
                    continue
                pending = list((await self._commit(session, candidate)).parents)
                visited: set[str] = set()
                while pending:
                    session.check()
                    node = pending.pop()
                    if node == candidate:
                        raise GitHubAppError("GitHub returned a cyclic commit graph")
                    if node in visited:
                        continue
                    visited.add(node)
                    stale.add(node)
                    if len(candidates - stale) == 1:
                        break
                    if node not in colors:
                        colors[node] = 3
                        if len(colors) > MAX_CHANGE_GRAPH_COMMITS:
                            raise GitHubAppError("Merge-base commit graph budget exhausted")
                    parents = graph.get(node)
                    if parents is None:
                        parents = (await self._commit(session, node)).parents
                        graph[node] = parents
                    pending.extend(parents)
                if len(candidates - stale) == 1:
                    break
            candidates -= stale
        if len(candidates) != 1:
            raise GitHubAppError("Cannot resolve one unambiguous PR merge base within the bounded commit graph")
        result = candidates.pop()
        self.cache.put(key, result)
        return result

    async def _inventory(
        self, session: _Session, base: str | None, head: str,
    ) -> list[dict[str, Any]]:
        key = self._key(session, "inventory", base, head)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        old_tree = (await self._commit(session, base)).tree if base else None
        new_tree = (await self._commit(session, head)).tree
        pending = [("", old_tree, new_tree, 0)]
        files: list[dict[str, Any]] = []

        def add(path: str, old: _Entry | None, new: _Entry | None) -> None:
            _path(path)
            status = "added" if old is None else "removed" if new is None else (
                "type_changed" if old.kind != new.kind or (old.mode == "120000") != (new.mode == "120000") else "modified"
            )
            files.append({
                "path": path, "status": status,
                "old_blob_sha": old.sha if old else None,
                "new_blob_sha": new.sha if new else None,
                "old_mode": old.mode if old else None, "new_mode": new.mode if new else None,
            })
            if len(files) > MAX_CHANGE_FILES:
                raise GitHubAppError("Changed-file inventory budget exhausted")

        while pending:
            session.check()
            prefix, old_sha, new_sha, depth = pending.pop()
            if old_sha == new_sha:
                continue
            if depth >= MAX_CHANGE_DEPTH:
                raise GitHubAppError("Change tree depth budget exhausted")
            old_entries = await self._tree(session, old_sha) if old_sha else {}
            new_entries = await self._tree(session, new_sha) if new_sha else {}
            for name in sorted(old_entries.keys() | new_entries.keys()):
                old, new = old_entries.get(name), new_entries.get(name)
                if old is not None and new is not None and old.sha == new.sha and old.mode == new.mode:
                    continue
                path = f"{prefix}/{name}" if prefix else name
                _path(path)
                old_dir, new_dir = old is not None and old.kind == "tree", new is not None and new.kind == "tree"
                if old_dir or new_dir:
                    pending.append((path, old.sha if old_dir else None, new.sha if new_dir else None, depth + 1))
                    if old is not None and not old_dir:
                        add(path, old, None)
                    if new is not None and not new_dir:
                        add(path, None, new)
                else:
                    add(path, old, new)
        files.sort(key=lambda item: item["path"])
        self.cache.put(key, files)
        return files

    async def list_change_files(
        self, repository: str, pull_number: int | None = None,
        commit_sha: str | None = None, base_sha: str | None = None,
        head_sha: str | None = None, cursor: str | None = None, per_page: int = 50,
    ) -> dict[str, Any]:
        repository = _repository(repository)
        _integer(per_page, "per_page", 1, 100)
        if pull_number is not None and all(value is None for value in (commit_sha, base_sha, head_sha)):
            _integer(pull_number, "pull_number", 1, 2**31 - 1)
            mode, target = "pull_request", f"pull:{pull_number}"
        elif commit_sha is not None and all(value is None for value in (pull_number, base_sha, head_sha)):
            commit_sha = _oid(commit_sha, "commit_sha")
            mode, target = "commit", f"commit:{commit_sha}"
        elif base_sha is not None and head_sha is not None and pull_number is None and commit_sha is None:
            base_sha, head_sha = _oid(base_sha, "base_sha"), _oid(head_sha, "head_sha")
            mode, target = "compare", f"compare:{base_sha}:{head_sha}"
        else:
            raise GitHubAppError("Supply exactly one change target: pull_number, commit_sha, or base_sha and head_sha")
        data = _decode_cursor(cursor, "l", {"r", "t", "b", "h", "a", "o", "s"})
        base_tip: str | None = None
        start = 0
        if data is not None:
            if data["r"] != repository.casefold() or data["t"] != target:
                raise GitHubAppError("Inventory cursor does not match this repository or target")
            pinned_base = _oid(data["b"], "cursor base SHA") if data["b"] is not None else None
            pinned_head = _oid(data["h"], "cursor head SHA")
            base_tip = _oid(data["a"], "cursor PR base tip") if data["a"] is not None else None
            start = _integer(data["o"], "cursor offset", 1, MAX_CHANGE_FILES)
            if (
                data["s"] != snapshot_id(repository, pinned_base, pinned_head)
                or (mode == "commit" and pinned_head != commit_sha)
                or (mode == "compare" and (pinned_base, pinned_head) != (base_sha, head_sha))
                or (mode == "pull_request") != (base_tip is not None)
            ):
                raise GitHubAppError("Inventory cursor has an inconsistent pinned identity")
            base_sha, head_sha = pinned_base, pinned_head
        async with self._session(repository) as session:
            if mode == "commit":
                commit = await self._commit(session, commit_sha)
                parent = commit.parents[0] if commit.parents else None
                if data is not None and parent != base_sha:
                    raise GitHubAppError("Inventory cursor does not match the commit's first parent")
                base_sha, head_sha = parent, commit.sha
            elif mode == "pull_request":
                if data is None:
                    pull = await session.get(f"/pulls/{pull_number}")
                    if (
                        not isinstance(pull, dict) or type(pull.get("number")) is not int
                        or pull["number"] != pull_number
                        or not isinstance(pull.get("base"), dict) or not isinstance(pull.get("head"), dict)
                    ):
                        raise GitHubAppError("GitHub returned an invalid pull request identity")
                    base_tip = _oid(pull["base"].get("sha"))
                    head_sha = _oid(pull["head"].get("sha"))
                resolved_base = await self._merge_base(session, base_tip, head_sha)
                if data is not None and resolved_base != base_sha:
                    raise GitHubAppError("Inventory cursor does not match the pinned PR merge base")
                base_sha = resolved_base
            files = await self._inventory(session, base_sha, head_sha)
            if start >= len(files) and data is not None:
                raise GitHubAppError("Inventory cursor is beyond the changed-file inventory")
            identity = snapshot_id(repository, base_sha, head_sha)
            snapshot = {"snapshot_id": identity, "base_sha": base_sha, "head_sha": head_sha, "mode": mode}
            if pull_number is not None:
                snapshot["pull_number"] = pull_number
            end = min(start + per_page, len(files))
            while True:
                continuation = _encode_cursor({
                    "k": "l", "r": repository.casefold(), "t": target,
                    "b": base_sha, "h": head_sha, "a": base_tip, "o": end, "s": identity,
                }) if end < len(files) else None
                result = {
                    "repository": repository, "snapshot": snapshot, "files": [dict(item) for item in files[start:end]],
                    "next_cursor": continuation, "complete": continuation is None,
                }
                if serialized_size(result) <= MAX_CHANGE_RESULT_BYTES:
                    return result
                end -= 1
                if end <= start:
                    raise GitHubAppError("Change inventory metadata exceeds the result limit")

    async def _entry(
        self, session: _Session, sha: str | None, path: str, *,
        non_tree_as_missing: bool = False,
    ) -> _Entry | None:
        if sha is None:
            return None
        tree = (await self._commit(session, sha)).tree
        parts = path.split("/")
        for index, name in enumerate(parts):
            entry = (await self._tree(session, tree)).get(name)
            if entry is None:
                return None
            if index == len(parts) - 1:
                return entry
            if entry.kind != "tree":
                if non_tree_as_missing:
                    return None
                raise GitHubAppError("Path has a non-directory ancestor; links are never followed")
            tree = entry.sha
        return None

    async def _blob(self, session: _Session, entry: _Entry) -> _Source:
        if entry.kind != "blob" or entry.mode not in {"100644", "100755"}:
            reason = {"120000": "symlink", "160000": "submodule", "040000": "directory"}.get(entry.mode, "unsupported_mode")
            return _Source("", array("I"), "unsupported", reason)
        if entry.size > MAX_CHANGE_BLOB_BYTES:
            return _Source("", array("I"), "unsupported", "blob_too_large")
        key = self._key(session, "blob", entry.sha, entry.size)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        payload = await session.get(f"/git/blobs/{entry.sha}")
        if (
            not isinstance(payload, dict) or _oid(payload.get("sha")) != entry.sha
            or type(payload.get("size")) is not int or payload["size"] != entry.size
            or not isinstance(payload.get("content"), str)
            or not isinstance(payload.get("encoding"), str)
        ):
            raise GitHubAppError("GitHub returned an invalid pinned blob")
        if payload["encoding"] != "base64":
            return _Source("", array("I"), "unsupported", "unsupported_encoding")
        compact = "".join(payload["content"].split())
        if len(compact) > 4 * ((entry.size + 2) // 3):
            raise GitHubAppError("Git blob exceeds its declared size")
        try:
            content = base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error):
            raise GitHubAppError("GitHub returned invalid Git blob base64") from None
        if len(content) != entry.size:
            raise GitHubAppError("Git blob differs from its declared size")
        actual_sha = hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content, usedforsecurity=False,
        ).hexdigest()
        if actual_sha != entry.sha:
            raise GitHubAppError("Git blob content does not match its immutable SHA")
        if any(byte < 32 and byte not in (9, 10, 12, 13) or byte == 127 for byte in content):
            source = _Source("", array("I"), "binary", "binary")
        else:
            try:
                source = _source(content.decode("utf-8"), len(content))
            except UnicodeDecodeError:
                source = _Source("", array("I"), "unsupported", "non_utf8")
        session.check()
        self.cache.put(key, source)
        return source

    async def _diff(self, session: _Session, base: str | None, head: str, path: str) -> _Diff:
        key = self._key(session, "diff", base, head, path)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        old = await self._entry(session, base, path, non_tree_as_missing=True)
        new = await self._entry(session, head, path, non_tree_as_missing=True)
        # Match the inventory's remove/add view of directory/file replacements.
        if old is not None and new is not None:
            if old.kind == "tree" and new.kind != "tree":
                old = None
            elif new.kind == "tree" and old.kind != "tree":
                new = None
        if old is None and new is None:
            raise GitHubAppError("Path does not exist in either pinned snapshot")
        old_sha, new_sha = old.sha if old else None, new.sha if new else None
        identity = _identity(
            "diff", snapshot_id(session.repository, base, head), path,
            old_sha, new_sha, old.mode if old else None, new.mode if new else None,
        )
        old_source = await self._blob(session, old) if old else _source("")
        new_source = await self._blob(session, new) if new else _source("")
        unsupported = next((source for source in (old_source, new_source) if source.status != "text"), None)
        if unsupported is not None:
            result = _Diff(identity, old_sha, new_sha, (), unsupported.status, unsupported.reason)
        else:
            result = _Diff(identity, old_sha, new_sha, _diff_parts(old, new, old_source, new_source, path))
        session.check()
        self.cache.put(key, result)
        return result

    async def read_diff_chunk(
        self, repository: str, base_sha: str | None, head_sha: str, path: str,
        cursor: str | None = None, max_bytes: int = DEFAULT_CHANGE_PAGE_BYTES,
    ) -> dict[str, Any]:
        repository, path = _repository(repository), _path(path)
        base_sha = _oid(base_sha, "base_sha") if base_sha is not None else None
        head_sha = _oid(head_sha, "head_sha")
        _integer(max_bytes, "max_bytes", MIN_CHANGE_PAGE_BYTES, MAX_CHANGE_RESULT_BYTES)
        snapshot = snapshot_id(repository, base_sha, head_sha)
        request_id = _identity("diff-request", snapshot, path)
        data = _page_cursor(cursor, "d", request_id)
        async with self._session(repository) as session:
            diff = await self._diff(session, base_sha, head_sha, path)
            if data is not None and data["i"] != diff.identity:
                raise GitHubAppError("Diff cursor does not match immutable blob identities")
            part_index, offset = (data["p"], data["o"]) if data else (0, 0)
            if diff.status != "text":
                if data is not None:
                    raise GitHubAppError("Nontext diffs have no continuation")
                result = {
                    "repository": repository, "snapshot_id": snapshot, "path": path,
                    "chunk_id": _identity("chunk", diff.identity, 0, 0, 0),
                    "content": "", "old_start": 0, "old_end": 0, "new_start": 0, "new_end": 0,
                    "representation": "notice", "status": diff.status, "reason": diff.reason,
                    "next_cursor": None, "complete": True,
                    "old_blob_sha": diff.old_sha, "new_blob_sha": diff.new_sha,
                }
                if serialized_size(result) > max_bytes:
                    raise GitHubAppError("max_bytes is too small for the result metadata")
                return result
            if (
                part_index >= len(diff.parts) or offset > len(diff.parts[part_index].content)
                or (data is not None and offset == len(diff.parts[part_index].content))
            ):
                raise GitHubAppError("Diff cursor is beyond the file's diff")
            part = diff.parts[part_index]

            def result_at(end: int) -> dict[str, Any]:
                next_part, next_offset = (part_index, end) if end < len(part.content) else (part_index + 1, 0)
                continuation = _encode_cursor({
                    "k": "d", "q": request_id, "i": diff.identity, "p": next_part, "o": next_offset,
                }) if next_part < len(diff.parts) else None
                old_start, old_end, new_start, new_end = part.ranges(offset, end)
                return {
                    "repository": repository, "snapshot_id": snapshot, "path": path,
                    "chunk_id": _identity("chunk", diff.identity, part.representation, part_index, offset, end),
                    "content": part.content[offset:end], "old_start": old_start, "old_end": old_end,
                    "new_start": new_start, "new_end": new_end, "representation": part.representation,
                    "status": "text", "next_cursor": continuation, "complete": continuation is None,
                    "old_blob_sha": diff.old_sha, "new_blob_sha": diff.new_sha,
                }

            return _fit_page(offset, len(part.content), max_bytes, result_at)

    async def read_file_range(
        self, repository: str, sha: str, path: str, start_line: int = 1,
        end_line: int = 120, cursor: str | None = None, max_bytes: int = DEFAULT_CHANGE_PAGE_BYTES,
    ) -> dict[str, Any]:
        repository, sha, path = _repository(repository), _oid(sha, "sha"), _path(path)
        _integer(start_line, "start_line", 1, MAX_CHANGE_BLOB_BYTES)
        _integer(end_line, "end_line", start_line, min(MAX_CHANGE_BLOB_BYTES, start_line + MAX_FILE_RANGE_LINES - 1))
        _integer(max_bytes, "max_bytes", MIN_CHANGE_PAGE_BYTES, MAX_CHANGE_RESULT_BYTES)
        request_id = _identity("file-request", repository.casefold(), sha, path, start_line, end_line)
        data = _page_cursor(cursor, "f", request_id)
        if data is not None and data["p"] != 0:
            raise GitHubAppError("Invalid file-range cursor part")
        async with self._session(repository) as session:
            entry = await self._entry(session, sha, path)
            if entry is None:
                raise GitHubAppError("Path does not exist in the pinned snapshot")
            source = await self._blob(session, entry)
            identity = _identity("file", request_id, entry.sha, entry.mode)
            if data is not None and data["i"] != identity:
                raise GitHubAppError("File cursor does not match the immutable blob identity")
            start = source.starts[start_line - 1] if start_line <= len(source.starts) else len(source.text)
            limit = source.starts[end_line] if end_line < len(source.starts) else len(source.text)
            offset = data["o"] if data else start
            if offset < start or offset > limit or (data is not None and offset == limit):
                raise GitHubAppError("File cursor is outside the requested source range")

            def result_at(end: int) -> dict[str, Any]:
                continuation = _encode_cursor({
                    "k": "f", "q": request_id, "i": identity, "p": 0, "o": end,
                }) if end < limit else None
                result = {
                    "repository": repository, "sha": sha, "path": path,
                    "chunk_id": _identity("chunk", identity, offset, end),
                    "content": source.text[offset:end],
                    "start_line": source.line_at(offset) if end > offset else 0,
                    "end_line": source.line_at(end - 1) if end > offset else 0,
                    "next_cursor": continuation, "complete": continuation is None,
                    "status": source.status,
                }
                if source.reason is not None:
                    result["reason"] = source.reason
                return result

            return _fit_page(offset, limit, max_bytes, result_at)


def _fit_page(start: int, limit: int, max_bytes: int, result_at: Any) -> dict[str, Any]:
    # Python string offsets are Unicode code points: even a single huge line
    # can continue without cutting a UTF-8 sequence or losing an escaped byte.
    end = min(limit, start + max_bytes)
    result = result_at(end)
    if serialized_size(result) <= max_bytes:
        return result
    low, high = start, end - 1
    best = None
    while low <= high:
        middle = (low + high) // 2
        candidate = result_at(middle)
        if serialized_size(candidate) <= max_bytes:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if best is None or (limit > start and not best["content"]):
        raise GitHubAppError("max_bytes is too small for the result metadata and one character")
    return best


def _diff_parts(
    old: _Entry | None, new: _Entry | None, before: _Source, after: _Source, path: str,
) -> tuple[_Part, ...]:
    old_lines, new_lines = len(before.starts), len(after.starts)
    total_chars = len(before.text) + len(after.text)
    expensive = (
        max(old_lines, new_lines) > MAX_DIFF_MATCH_LINES
        or old_lines * new_lines > MAX_DIFF_MATCH_CELLS
        or before.byte_size + after.byte_size > MAX_DIFF_MATCH_INPUT_BYTES
        or total_chars * max(old_lines, new_lines, 1) > MAX_DIFF_MATCH_WORK
    )
    if expensive and before.text != after.text:
        parts = []
        if before.text:
            parts.append(_Part(before.text, "replacement-old", before))
        if after.text:
            parts.append(_Part(after.text, "replacement-new", after))
        return tuple(parts)
    spans: list[tuple[str, int, int]] = []

    def emit(content: str, old_line: int = 0, new_line: int = 0) -> None:
        if content:
            spans.append((content, old_line, new_line))

    if old == new:
        return (_Part("", "unified"),)
    emit(f"--- {'a/' + path if old else '/dev/null'}\n")
    emit(f"+++ {'b/' + path if new else '/dev/null'}\n")
    if old is None or new is None or old.mode != new.mode:
        emit(f"old mode {old.mode if old else 'absent'}\nnew mode {new.mode if new else 'absent'}\n")
    if before.text != after.text:
        before_lines, after_lines = before.lines(), after.lines()
        matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
        for group in matcher.get_grouped_opcodes(3):
            first, last = group[0], group[-1]
            emit(f"@@ -{_unified_range(first[1], last[2])} +{_unified_range(first[3], last[4])} @@\n")
            for tag, i1, i2, j1, j2 in group:
                if tag in {"equal", "delete", "replace"}:
                    for index in range(i1, i2):
                        text = before_lines[index]
                        emit((" " if tag == "equal" else "-") + text, index + 1, j1 + index - i1 + 1 if tag == "equal" else 0)
                        if not text.endswith("\n"):
                            emit("\n\\ No newline at end of file\n")
                if tag in {"insert", "replace"}:
                    for index in range(j1, j2):
                        text = after_lines[index]
                        emit("+" + text, 0, index + 1)
                        if not text.endswith("\n"):
                            emit("\n\\ No newline at end of file\n")
    content = "".join(span[0] for span in spans)
    offset = 0
    ends = []
    for text, _, _ in spans:
        offset += len(text)
        ends.append(offset)
    return (_Part(
        content, "unified", ends=tuple(ends),
        old_lines=tuple(span[1] for span in spans), new_lines=tuple(span[2] for span in spans),
    ),)


def _unified_range(start: int, end: int) -> str:
    length = end - start
    beginning = start + 1 if length else start
    return str(beginning) if length == 1 else f"{beginning},{length}"
