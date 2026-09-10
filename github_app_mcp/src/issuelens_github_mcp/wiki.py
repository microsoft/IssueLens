"""Bounded in-process Git wiki access, with no checkout or ambient config.

Budgets bound wire, raw object and expanded delta bytes, not Python allocator
overhead. Deadlines are checked at transport reads and object/traversal steps;
DNS resolution and HTTP header parsing retain the socket/library timeouts.
"""

from __future__ import annotations

import base64
import difflib
import functools
import hashlib
import io
import math
import os
import re
import shutil
import socket
import stat
import struct
import tempfile
import time
import unicodedata
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Literal, ParamSpec, Protocol, TypedDict, TypeVar

import urllib3
from urllib3.response import HTTPResponse
from dulwich.client import GitClient, HttpGitClient
from dulwich.config import ConfigDict
from dulwich.objects import Blob, Commit, ObjectID as APIObjectID, ShaFile, Tree
from dulwich.object_store import MemoryObjectStore
from dulwich.pack import apply_delta
from dulwich.refs import Ref, check_ref_format
from dulwich.repo import MemoryRepo
from dulwich.walk import ORDER_TOPO, Walker

from .auth import GitHubAppError, validate_repository


class WikiError(RuntimeError):
    """A bounded, credential-free wiki failure."""


_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_REGULAR_MODES = {"100644", "100755"}
_RESERVED = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])(?:\.|$)", re.I)

Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")
ObjectType = TypeVar("ObjectType", bound=ShaFile)


class _WikiSnapshot(TypedDict):
    repository: str
    branch: str
    sha: str | None
    initialized: bool


class _WikiWriteResult(TypedDict):
    status: Literal["no-change", "updated"]
    sha: str
    branch: str
    pages: list[str]
    repository: str


class _PackStream(Protocol):
    def read(self, size: int = -1, /) -> bytes:
        ...

    def seek(self, offset: int, whence: int = 0, /) -> int:
        ...

    def tell(self) -> int:
        ...


def _safe(function: Callable[Parameters, Result]) -> Callable[Parameters, Result]:
    @functools.wraps(function)
    def guarded(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
        try:
            return function(*args, **kwargs)
        except WikiError:
            raise
        except Exception:
            raise WikiError("wiki operation failed or exceeded its budget") from None
    return guarded


def _path(value: str, *, markdown: bool = True) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise WikiError("invalid wiki path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise WikiError("wiki paths must be UTF-8") from None
    if len(encoded) > 240 or any(unicodedata.category(char).startswith("C") for char in value):
        raise WikiError("invalid wiki path")
    if any(char in value for char in '\\:<>"|?*'):
        raise WikiError("unsafe wiki path")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part.startswith("-")
                or part.endswith((" ", ".")) or part.casefold() == ".git"
                or re.fullmatch(r"git~[0-9]+", part, re.I) or _RESERVED.match(part)):
            raise WikiError("unsafe wiki path")
    if markdown and not value.casefold().endswith(".md"):
        raise WikiError("wiki pages must be Markdown")
    return value


def _ref(value: str) -> str:
    if not isinstance(value, str) or (value != "HEAD" and not _SHA.fullmatch(value)):
        raise WikiError("wiki ref must be HEAD or a full commit SHA")
    return value


def _object_id(value: bytes) -> APIObjectID:
    if not isinstance(value, bytes) or not re.fullmatch(b"[0-9a-f]{40}", value):
        raise WikiError("wiki supports SHA-1 repositories only")
    return APIObjectID(value)


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeError:
        raise WikiError("wiki content must be UTF-8") from None


def _compatible_paths(paths: list[str], directories: Iterable[str] = ()) -> None:
    components: dict[str, tuple[str, bool]] = {}
    for path, leaf_is_file in [(path, True) for path in paths] + [(path, False) for path in directories]:
        _path(path, markdown=False)
        parts = path.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = unicodedata.normalize("NFC", prefix).casefold()
            is_file = leaf_is_file and length == len(parts)
            previous = components.get(key)
            if previous is not None and previous != (prefix, is_file):
                raise WikiError("wiki paths have a case, Unicode, or file/directory collision")
            components[key] = (prefix, is_file)


def _summary(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or not value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
        raise WikiError(f"invalid wiki {name}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise WikiError(f"invalid wiki {name}") from None
    if size > maximum:
        raise WikiError(f"wiki {name} exceeds byte limit")
    return value


class _WikiHttpResponse:
    def __init__(self, response: HTTPResponse, content_type: str, read: Callable[[int], bytes]) -> None:
        self._response = response
        self._read = read
        self.status: int = response.status
        self.headers: Mapping[str, str] = response.headers
        self.content_type: str | None = content_type
        self.redirect_location: str = ""

    @property
    def closed(self) -> bool:
        return self._response.closed

    def close(self) -> None:
        self._response.close()

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            raise WikiError("wiki transport read budget exceeded")
        return self._read(amt)

    def geturl(self) -> str | None:
        return self._response.geturl()


class _WikiHttpClient(HttpGitClient):
    def __init__(self, wiki: WikiRepository, base_url: str) -> None:
        self._wiki = wiki
        self._responses: list[HTTPResponse] = []
        self._allowed_urls = {
            base_url + "/info/refs?service=git-upload-pack",
            base_url + "/info/refs?service=git-receive-pack",
            base_url + "/git-upload-pack", base_url + "/git-receive-pack",
        }
        super().__init__(base_url, config=ConfigDict(), thin_packs=True,
                         pool_manager=urllib3.PoolManager(cert_reqs="CERT_REQUIRED", retries=False))

    def close(self) -> None:
        for response in self._responses:
            response.close()
        self._responses.clear()
        self.pool_manager.clear()

    def _http_request(
        self, url: str, headers: dict[str, str] | None = None,
        data: bytes | Iterator[bytes] | None = None, raise_for_status: bool = True,
    ) -> tuple[_WikiHttpResponse, Callable[[int], bytes]]:
        wiki = self._wiki
        wiki._request()
        if url not in self._allowed_urls:
            raise WikiError("wiki transport URL is not allowed")
        metadata = not url.endswith("/git-upload-pack") or (isinstance(data, bytes) and b"command=ls-refs\n" in data)
        response_limit = wiki.MAX_OUTPUT_BYTES if metadata else wiki.MAX_DISK_BYTES
        response_bytes = 0
        request_headers = dict(headers or {})
        request_headers.update({"Accept-Encoding": "identity", "Pragma": "no-cache"})
        if wiki._token:
            credential = base64.b64encode(("x-access-token:" + wiki._token).encode("ascii")).decode("ascii")
            request_headers["Authorization"] = "Basic " + credential
        try:
            with tempfile.TemporaryFile(dir=wiki._parent) as body:
                if data is not None:
                    size = 0
                    for chunk in ([data] if isinstance(data, bytes) else data):
                        wiki._check_budget()
                        size += len(chunk)
                        wiki._wire_bytes += len(chunk)
                        if size > wiki.MAX_DISK_BYTES or wiki._wire_bytes > wiki.MAX_DISK_BYTES:
                            raise WikiError("wiki transport byte budget exceeded")
                        body.write(chunk)
                    body.seek(0)
                    request_headers["Content-Length"] = str(size)
                remaining = wiki._remaining()
                response = self.pool_manager.request(
                    "GET" if data is None else "POST", url, headers=request_headers,
                    body=None if data is None else body, redirect=False, retries=False,
                    preload_content=False, decode_content=False,
                    timeout=urllib3.Timeout(total=remaining, connect=min(5, remaining), read=min(5, remaining)),
                )
            if not isinstance(response, HTTPResponse):
                if response is not None:
                    response.close()
                raise WikiError("wiki HTTP response is invalid")
            self._responses.append(response)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            if (response.status != 200 or not content_type.startswith("application/x-git-")
                    or response.headers.get("Content-Encoding", "identity") != "identity"):
                response.close()
                raise WikiError("wiki HTTP request rejected (redirects and dumb HTTP are disabled)")
        except WikiError:
            raise
        except Exception:
            raise WikiError("wiki HTTP request failed or timed out") from None

        def read(size: int) -> bytes:
            nonlocal response_bytes
            if not isinstance(size, int) or not 0 <= size <= 65536:
                raise WikiError("wiki transport read budget exceeded")
            result = bytearray()
            try:
                while len(result) < size:
                    remaining = wiki._remaining()
                    connection = response.connection
                    sock = getattr(connection, "sock", None)
                    if sock is None:
                        raw = getattr(getattr(getattr(response, "_fp", None), "fp", None), "raw", None)
                        sock = getattr(raw, "_sock", None)
                    if isinstance(sock, socket.socket):
                        sock.settimeout(min(5, remaining))
                    chunk = response.read1(min(size - len(result), 16384), decode_content=False)
                    wiki._wire_bytes += len(chunk)
                    response_bytes += len(chunk)
                    if response_bytes > response_limit:
                        raise WikiError("wiki transport response byte budget exceeded")
                    if wiki._wire_bytes > wiki.MAX_DISK_BYTES:
                        raise WikiError("wiki transport byte budget exceeded")
                    wiki._check_budget()
                    if not chunk:
                        break
                    result.extend(chunk)
                return bytes(result)
            except WikiError:
                response.close()
                raise
            except Exception:
                response.close()
                raise WikiError("wiki HTTP read failed or timed out") from None
        return _WikiHttpResponse(response, content_type, read), read


class _BoundedStore(MemoryObjectStore):
    def __init__(self, wiki: WikiRepository) -> None:
        super().__init__()
        self.wiki = wiki
        self.raw_bytes = 0

    def add_object(self, obj: ShaFile) -> None:
        self.wiki._check_budget()
        raw = obj.as_raw_string()
        if len(raw) > self.wiki.MAX_OBJECT_BYTES:
            raise WikiError("wiki object byte budget exceeded")
        if obj.type_num != Blob.type_num and len(raw) > self.wiki.MAX_READ_BYTES:
            raise WikiError("wiki structural object byte budget exceeded")
        obj.check()
        if isinstance(obj, Commit) and len(obj.parents) > 64:
            raise WikiError("wiki commit parent budget exceeded")
        if obj.id not in self:
            if (self.raw_bytes + len(raw) > self.wiki.MAX_DISK_BYTES
                    or len(self._data) >= self.wiki.MAX_DISK_ENTRIES):
                raise WikiError("wiki object storage budget exceeded")
            self.raw_bytes += len(raw)
            super().add_object(obj)


class WikiRepository:
    """Snapshot-pinned Git objects fetched only through a derived HTTPS remote."""

    MAX_DISK_BYTES = 64 * 1024 * 1024
    MAX_DISK_ENTRIES = 20_000
    MAX_OBJECT_BYTES = 8 * 1024 * 1024
    MAX_DELTA_DEPTH = 64
    MAX_DELTA_OPS = 20_000
    MAX_REQUESTS = 32
    MAX_OUTPUT_BYTES = 512 * 1024
    MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
    MAX_TREE_ENTRIES = 2048
    MAX_PAGES = 200
    MAX_PAGE_BYTES = 64 * 1024
    MAX_READ_BYTES = 256 * 1024
    MAX_BATCH_PAGES = 20
    MAX_BATCH_BYTES = 256 * 1024

    def __init__(self, repository: str, *, token: str | None = None, timeout: int = 30) -> None:
        try:
            if not isinstance(repository, str) or len(repository) > 140:
                raise ValueError
            self.repository = validate_repository(repository)
            if self.repository.split("/", 1)[1] in {".", ".."}:
                raise ValueError
        except (GitHubAppError, ValueError):
            raise WikiError("repository must use owner/repository format") from None
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise WikiError("timeout must be between zero (exclusive) and 300 seconds")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise WikiError("invalid wiki credential")
        self.timeout = timeout
        self._token = token
        self._parent: Path | None = None
        self._root = None
        self._repo: MemoryRepo | None = None
        self._object_store: _BoundedStore | None = None
        self._client: GitClient | None = None
        self._transport_path = b""
        self._sha: str | None = None
        self._branch = ""
        self._deadline = 0.0
        self._wire_bytes = self._requests = self._output_bytes = 0
        self._reachable: set[APIObjectID] = set()

    @property
    def remote(self) -> str:
        return f"https://github.com/{self.repository}.wiki.git"

    @property
    def _repository(self) -> MemoryRepo:
        if self._repo is None:
            raise WikiError("wiki repository is not open")
        return self._repo

    @property
    def _store(self) -> _BoundedStore:
        if self._object_store is None:
            raise WikiError("wiki repository is not open")
        return self._object_store

    @property
    def _transport(self) -> GitClient:
        if self._client is None:
            raise WikiError("wiki repository is not open")
        return self._client

    def _get_object(self, sha: APIObjectID, expected_type: type[ObjectType]) -> ObjectType:
        obj = self._store[sha]
        if not isinstance(obj, expected_type):
            raise WikiError("invalid wiki history object")
        return obj

    def _create_client(self) -> tuple[GitClient, str]:
        remote = f"https://github.com/{self.repository}.wiki.git"
        return _WikiHttpClient(self, remote), remote

    @_safe
    def __enter__(self) -> WikiRepository:
        if self._parent is not None:
            raise WikiError("wiki repository is already open")
        self._deadline = time.monotonic() + self.timeout
        self._wire_bytes = self._requests = self._output_bytes = 0
        self._reachable.clear()
        self._branch = ""
        try:
            self._parent = Path(tempfile.mkdtemp(prefix="issuelens-wiki-"))
            self._repo = MemoryRepo()
            self._object_store = _BoundedStore(self)
            self._repo.object_store = self._object_store
            self._client, transport_path = self._create_client()
            self._transport_path = transport_path.encode("utf-8")
            self._sha = self._fetch(initial=True)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    @_safe
    def __exit__(self, *_: object) -> None:
        def remove_readonly(function, path, error):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        try:
            if self._client is not None and hasattr(self._client, "close"):
                self._client.close()
        finally:
            try:
                if self._repo is not None:
                    self._repo.close()
            finally:
                try:
                    if self._parent is not None:
                        shutil.rmtree(self._parent, onerror=remove_readonly)
                        self._parent = None
                except OSError:
                    raise WikiError("wiki temporary storage cleanup failed") from None
                finally:
                    self._repo = self._client = self._root = None
                    self._object_store = None
                    self._transport_path = b""
                    self._sha = None
                    self._reachable.clear()

    def _check_budget(self) -> None:
        if self._parent is None:
            raise WikiError("wiki repository is not open")
        if time.monotonic() >= self._deadline:
            raise WikiError("wiki operation exceeded time budget")

    def _remaining(self) -> float:
        self._check_budget()
        return max(0.001, self._deadline - time.monotonic())

    def _request(self) -> None:
        self._check_budget()
        self._requests += 1
        if self._requests > self.MAX_REQUESTS:
            raise WikiError("wiki transport request budget exceeded")

    def _progress(self, message: bytes) -> None:
        self._check_budget()
        self._output_bytes += len(message)
        if self._output_bytes > self.MAX_TOTAL_OUTPUT_BYTES:
            raise WikiError("wiki output budget exceeded")

    def _delta_size(self, data: bytes | bytearray, index: int) -> tuple[int, int]:
        value = 0
        for shift in range(0, 35, 7):
            if index >= len(data):
                raise WikiError("invalid wiki pack delta")
            byte = data[index]
            index += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                if value > self.MAX_OBJECT_BYTES:
                    raise WikiError("wiki delta object byte budget exceeded")
                return value, index
        raise WikiError("invalid wiki pack delta")

    def _validate_delta(self, data: bytes | bytearray) -> tuple[int, int]:
        source_size, index = self._delta_size(data, 0)
        target_size, index = self._delta_size(data, index)
        produced = operations = 0
        while index < len(data):
            self._check_budget()
            operations += 1
            if operations > self.MAX_DELTA_OPS:
                raise WikiError("wiki delta instruction budget exceeded")
            command = data[index]
            index += 1
            if command & 128:
                offset = size = 0
                for bit in range(7):
                    if command & (1 << bit):
                        if index >= len(data):
                            raise WikiError("invalid wiki pack delta")
                        if bit < 4:
                            offset |= data[index] << (8 * bit)
                        else:
                            size |= data[index] << (8 * (bit - 4))
                        index += 1
                size = size or 65536
                if offset + size > source_size:
                    raise WikiError("invalid wiki pack delta")
            elif command:
                size = command
                index += size
            else:
                raise WikiError("invalid wiki pack delta")
            produced += size
            if index > len(data) or produced > target_size:
                raise WikiError("invalid wiki pack delta")
        if produced != target_size:
            raise WikiError("invalid wiki pack delta")
        return source_size, target_size

    def _load_pack(self, stream: _PackStream, size: int) -> None:
        stream.seek(0)
        digest = hashlib.sha1(usedforsecurity=False)
        remaining = size - 20
        if remaining < 12:
            raise WikiError("invalid wiki pack")
        while remaining:
            self._check_budget()
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise WikiError("invalid wiki pack")
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(20) != digest.digest():
            raise WikiError("invalid wiki pack checksum")
        stream.seek(0)
        signature, version, count = struct.unpack(">4sII", stream.read(12))
        if signature != b"PACK" or version not in (2, 3):
            raise WikiError("unsupported wiki pack format")
        if count > self.MAX_DISK_ENTRIES:
            raise WikiError("wiki object count budget exceeded")
        offsets: dict[int, APIObjectID] = {}
        depths: dict[APIObjectID, int] = {}
        pending: list[tuple[int, int | APIObjectID, bytes, int, int]] = []
        expanded = self._store.raw_bytes + size

        def byte() -> int:
            value = stream.read(1)
            if not value:
                raise WikiError("invalid wiki pack")
            return value[0]

        for unused in range(count):
            self._check_budget()
            offset = stream.tell()
            current = byte()
            kind, length = (current >> 4) & 7, current & 15
            shift = 4
            while current & 128:
                if shift > 32:
                    raise WikiError("wiki object byte budget exceeded")
                current = byte()
                length |= (current & 127) << shift
                shift += 7
            if kind not in (1, 2, 3, 4, 6, 7) or length > self.MAX_OBJECT_BYTES:
                raise WikiError("wiki object byte budget exceeded or invalid type")
            if kind in (1, 2, 4) and length > self.MAX_READ_BYTES:
                raise WikiError("wiki structural object byte budget exceeded")
            base: int | APIObjectID | None = None
            if kind == 6:
                current = byte()
                distance = current & 127
                for unused_byte in range(5):
                    if not current & 128:
                        break
                    current = byte()
                    distance = ((distance + 1) << 7) + (current & 127)
                if current & 128 or distance <= 0 or distance > offset - 12:
                    raise WikiError("invalid wiki delta base")
                base = offset - distance
            elif kind == 7:
                base = _object_id(stream.read(20).hex().encode("ascii"))
            expanded += length
            if expanded > self.MAX_DISK_BYTES:
                raise WikiError("wiki expanded object storage budget exceeded")
            inflater = zlib.decompressobj()
            raw = bytearray()
            while not inflater.eof:
                self._check_budget()
                compressed = inflater.unconsumed_tail or stream.read(65536)
                if not compressed:
                    raise WikiError("invalid wiki compressed object")
                raw.extend(inflater.decompress(compressed, min(65536, length - len(raw) + 1)))
                if len(raw) > length:
                    raise WikiError("wiki expanded object byte budget exceeded")
            stream.seek(-len(inflater.unused_data), os.SEEK_CUR)
            if len(raw) != length:
                raise WikiError("invalid wiki object size")
            if kind in (6, 7):
                if base is None:
                    raise WikiError("invalid wiki delta base")
                source_size, target_size = self._validate_delta(raw)
                expanded += target_size
                if expanded > self.MAX_DISK_BYTES:
                    raise WikiError("wiki expanded object storage budget exceeded")
                pending.append((offset, base, bytes(raw), source_size, target_size))
            else:
                obj = ShaFile.from_raw_string(kind, bytes(raw))
                self._store.add_object(obj)
                offsets[offset] = obj.id
                depths[obj.id] = 0
        if stream.tell() != size - 20:
            raise WikiError("invalid wiki pack trailing data")
        while pending:
            unresolved: list[tuple[int, int | APIObjectID, bytes, int, int]] = []
            for offset, base, delta, source_size, target_size in pending:
                self._check_budget()
                base_id = offsets.get(base) if isinstance(base, int) else base
                if base_id is None or base_id not in self._store:
                    unresolved.append((offset, base, delta, source_size, target_size))
                    continue
                depth = depths.get(base_id, 0) + 1
                if depth > self.MAX_DELTA_DEPTH:
                    raise WikiError("wiki delta depth budget exceeded")
                kind, source = self._store.get_raw(base_id)
                if len(source) != source_size:
                    raise WikiError("invalid wiki delta source size")
                if kind != Blob.type_num and target_size > self.MAX_READ_BYTES:
                    raise WikiError("wiki structural object byte budget exceeded")
                obj = ShaFile.from_raw_string(kind, b"".join(apply_delta(source, delta)))
                self._store.add_object(obj)
                offsets[offset] = obj.id
                depths[obj.id] = max(depths.get(obj.id, 0), depth)
            if len(unresolved) == len(pending):
                raise WikiError("wiki delta base unavailable")
            pending = unresolved

    def _branch_name(self, branch_ref: bytes) -> str:
        if not isinstance(branch_ref, bytes) or not branch_ref.startswith(b"refs/heads/"):
            raise WikiError("wiki default branch is unavailable")
        branch = _text(branch_ref[len(b"refs/heads/"):])
        if (not branch or len(branch.encode("utf-8")) > 200 or not check_ref_format(Ref(branch_ref))
                or any(unicodedata.category(char).startswith("C") for char in branch)):
            raise WikiError("invalid wiki default branch")
        return branch

    def _fetch(self, *, initial: bool = False) -> str | None:
        self._request()
        with tempfile.TemporaryFile(dir=self._parent) as stream:
            size = 0

            def write(chunk: bytes) -> int:
                nonlocal size
                self._check_budget()
                size += len(chunk)
                if size > self.MAX_DISK_BYTES:
                    raise WikiError("wiki pack storage budget exceeded")
                return stream.write(chunk)

            def wants(refs: Mapping[Ref, APIObjectID], depth: int | None = None) -> list[APIObjectID]:
                target = Ref(b"HEAD" if initial else ("refs/heads/" + self._branch).encode("utf-8"))
                sha = refs.get(target)
                if sha is None:
                    if refs:
                        raise WikiError("wiki default branch is unavailable")
                    return []
                sha = _object_id(sha)
                return [] if sha in self._reachable else [sha]

            result = self._transport.fetch_pack(self._transport_path, wants, self._repository.get_graph_walker(),
                                                write, progress=self._progress, protocol_version=2)
            symrefs = result.symrefs
            if symrefs is None:
                if result.refs:
                    raise WikiError("wiki default branch is unavailable")
                symrefs = {}
            if initial:
                advertised_branch = symrefs.get(Ref(b"HEAD"))
                if advertised_branch is not None:
                    self._branch = self._branch_name(advertised_branch)
                elif result.refs:
                    raise WikiError("wiki default branch is unavailable")
            branch_ref = Ref(("refs/heads/" + self._branch).encode("utf-8"))
            current = result.refs.get(branch_ref)
            if result.refs and (symrefs.get(Ref(b"HEAD")) != branch_ref
                                or result.refs.get(Ref(b"HEAD")) != current):
                raise WikiError("wiki default branch advertisement changed")
            if size:
                self._load_pack(stream, size)
        if current is None:
            if not initial:
                raise WikiError("wiki base conflict; remote branch is missing")
            return None
        current = _object_id(current)
        self._mark_history(current)
        self._repository.refs[branch_ref] = current
        return current.decode("ascii")

    def _mark_history(self, sha: APIObjectID) -> None:
        pending = [sha]
        expected_types: dict[APIObjectID, type[ShaFile]] = {sha: Commit}
        reachable: set[APIObjectID] = set()
        while pending:
            self._check_budget()
            current = pending.pop()
            expected_type = expected_types[current]
            if expected_type is Commit and current in self._reachable:
                continue
            obj = self._store[current]
            if not isinstance(obj, expected_type):
                raise WikiError("invalid wiki history object")
            references: Iterable[tuple[APIObjectID, type[ShaFile]]]
            if isinstance(obj, Commit):
                reachable.add(current)
                references = [(obj.tree, Tree)] + [(parent, Commit) for parent in obj.parents]
            elif isinstance(obj, Tree):
                references = ((entry.sha, Tree if entry.mode == 0o40000 else Blob)
                              for entry in obj.iteritems() if entry.mode != 0o160000)
            else:
                references = ()
            for child, child_type in references:
                self._check_budget()
                if child in expected_types:
                    if expected_types[child] is not child_type:
                        raise WikiError("invalid wiki history object")
                    continue
                expected_types[child] = child_type
                if len(expected_types) > self.MAX_DISK_ENTRIES:
                    raise WikiError("wiki history traversal budget exceeded")
                pending.append(child)
        self._reachable.update(reachable)

    def _resolve(self, ref: str) -> str:
        self._check_budget()
        ref = _ref(ref)
        if ref == "HEAD":
            if self._sha is None:
                raise WikiError("wiki has no initialized snapshot")
            return self._sha
        ref = ref.lower()
        if len(ref) != 40 or _object_id(ref.encode("ascii")) not in self._reachable:
            raise WikiError("wiki commit unavailable; only fetched SHA-1 history is supported")
        return ref

    @_safe
    def snapshot(self) -> _WikiSnapshot:
        self._check_budget()
        return {"repository": self.repository, "branch": self._branch, "sha": self._sha, "initialized": self._sha is not None}

    def _inventory(self, sha: str, *, include_trees: bool = False) -> dict[str, tuple[str, str, int]]:
        commit = self._get_object(_object_id(sha.encode("ascii")), Commit)
        pending = [(b"", commit.tree, 0)]
        result: dict[str, tuple[str, str, int]] = {}
        directories: dict[str, tuple[str, str, int]] = {}
        count = 0
        while pending:
            self._check_budget()
            prefix, tree_id, depth = pending.pop()
            if depth > 120:
                raise WikiError("wiki tree depth budget exceeded")
            tree = self._store[tree_id]
            if not isinstance(tree, Tree):
                raise WikiError("invalid wiki tree")
            for entry in tree.iteritems():
                count += 1
                if count > self.MAX_TREE_ENTRIES:
                    raise WikiError("wiki tree entry limit exceeded")
                if b"/" in entry.path or not entry.path:
                    raise WikiError("invalid wiki tree path")
                path = prefix + entry.path
                _path(_text(path), markdown=False)
                if entry.mode == 0o40000:
                    directories[_text(path)] = ("040000", entry.sha.decode("ascii"), -1)
                    pending.append((path + b"/", entry.sha, depth + 1))
                else:
                    size = -1
                    if entry.mode != 0o160000:
                        blob = self._store[entry.sha]
                        if not isinstance(blob, Blob):
                            raise WikiError("invalid wiki blob")
                        size = blob.raw_length()
                    result[_text(path)] = (f"{entry.mode:06o}", entry.sha.decode("ascii"), size)
        _compatible_paths(list(result), directories)
        if include_trees:
            result.update(directories)
        return dict(sorted(result.items()))

    def _pages(self, entries: dict[str, tuple[str, str, int]]) -> list[dict[str, str]]:
        result = [{"path": _path(path), "sha": oid} for path, (mode, oid, _) in entries.items()
                  if path.casefold().endswith(".md") and mode in _REGULAR_MODES]
        if len(result) > self.MAX_PAGES:
            raise WikiError("wiki page count limit exceeded")
        return result

    @_safe
    def pages(self, ref: str = "HEAD") -> list[dict[str, str]]:
        return self._pages(self._inventory(self._resolve(ref)))

    @_safe
    def page(self, path: str, ref: str = "HEAD") -> dict[str, str]:
        path = _path(path)
        sha = self._resolve(ref)
        entry = self._inventory(sha).get(path)
        if entry is None or entry[0] not in _REGULAR_MODES:
            raise WikiError("wiki page is missing or is not a regular file")
        if entry[2] > self.MAX_PAGE_BYTES:
            raise WikiError("wiki page byte limit exceeded")
        content = _text(self._get_object(_object_id(entry[1].encode("ascii")), Blob).data)
        return {"path": path, "ref": sha, "sha": entry[1], "content": content}

    @_safe
    def search(self, query: str, ref: str = "HEAD") -> list[dict[str, str]]:
        _summary(query, "query", 512)
        sha = self._resolve(ref)
        entries = self._inventory(sha)
        pages = self._pages(entries)
        total = 0
        for page in pages:
            size = entries[page["path"]][2]
            if size > self.MAX_PAGE_BYTES:
                raise WikiError("wiki page byte limit exceeded")
            total += size
        if total > self.MAX_READ_BYTES:
            raise WikiError("wiki search byte limit exceeded")
        result = []
        for page in pages:
            self._check_budget()
            content = _text(self._get_object(_object_id(page["sha"].encode("ascii")), Blob).data)
            if query.casefold() in content.casefold():
                result.append(page)
        return result

    def _ancestors(self, sha: APIObjectID) -> Iterator[tuple[APIObjectID, Commit]]:
        pending = [sha]
        discovered = {sha}
        while pending:
            self._check_budget()
            current = pending.pop()
            commit = self._store[current]
            if not isinstance(commit, Commit):
                raise WikiError("wiki commit unavailable")
            yield current, commit
            for parent in reversed(commit.parents):
                if parent not in discovered:
                    discovered.add(parent)
                    if len(discovered) > self.MAX_DISK_ENTRIES:
                        raise WikiError("wiki history traversal budget exceeded")
                    pending.append(parent)

    @_safe
    def history(self, path: str | None = None, limit: int = 30, ref: str = "HEAD") -> list[str]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WikiError("history limit must be between 1 and 100")
        if path is not None:
            path = _path(path)
        sha = self._resolve(ref)
        if path is not None:
            entry = self._inventory(sha).get(path)
            if entry is not None and entry[0] not in _REGULAR_MODES:
                raise WikiError("wiki page is not a regular file")
        start = _object_id(sha.encode("ascii"))
        discovered = {start}

        def get_parents(commit: Commit) -> list[APIObjectID]:
            self._check_budget()
            discovered.update(commit.parents)
            if len(discovered) > self.MAX_DISK_ENTRIES:
                raise WikiError("wiki history traversal budget exceeded")
            return commit.parents

        result = []
        for walked in Walker(self._store, [start], order=ORDER_TOPO, get_parents=get_parents):
            self._check_budget()
            commit = walked.commit
            current = commit.id
            changed = True
            if path is not None:
                entry = self._inventory(current.decode("ascii")).get(path)
                parents = [self._inventory(parent.decode("ascii")).get(path) for parent in commit.parents]
                changed = entry is not None if not parents else all(entry != previous for previous in parents)
            if changed:
                result.append(current.decode("ascii"))
                if len(result) == limit:
                    break
        return result

    @_safe
    def diff(self, base: str, head: str = "HEAD") -> str:
        _ref(base)
        _ref(head)
        before = self._inventory(self._resolve(base))
        after = self._inventory(self._resolve(head))
        output = bytearray()
        read_bytes = 0

        def emit(text: str) -> None:
            self._check_budget()
            encoded = text.encode("utf-8")
            if len(output) + len(encoded) > self.MAX_OUTPUT_BYTES:
                raise WikiError("wiki output budget exceeded")
            output.extend(encoded)

        for path in sorted(set(before) | set(after)):
            old, new = before.get(path), after.get(path)
            if old == new:
                continue
            emit(f"diff --git a/{path} b/{path}\n")
            if old is None:
                if new is None:
                    raise WikiError("invalid wiki diff entry")
                emit(f"new file mode {new[0]}\n")
            elif new is None:
                emit(f"deleted file mode {old[0]}\n")
            elif old[0] != new[0]:
                emit(f"old mode {old[0]}\nnew mode {new[0]}\n")
            contents = []
            for entry in (old, new):
                if entry is None:
                    contents.append(b"")
                elif entry[0] == "160000":
                    contents.append(("Subproject commit " + entry[1] + "\n").encode("ascii"))
                elif entry[2] > self.MAX_READ_BYTES:
                    raise WikiError("wiki diff read byte budget exceeded")
                else:
                    read_bytes += entry[2]
                    if read_bytes > self.MAX_READ_BYTES:
                        raise WikiError("wiki diff read byte budget exceeded")
                    contents.append(self._get_object(_object_id(entry[1].encode("ascii")), Blob).data)
            if any(b"\0" in content for content in contents):
                emit(f"Binary files a/{path} and b/{path} differ\n")
                continue
            try:
                old_text, new_text = [content.decode("utf-8") for content in contents]
            except UnicodeError:
                emit(f"Binary files a/{path} and b/{path} differ\n")
                continue
            old_lines, new_lines = [list(io.StringIO(text, newline="\n")) for text in (old_text, new_text)]
            if len(old_lines) * len(new_lines) > 4_000_000:
                raise WikiError("wiki diff comparison budget exceeded")
            for line in difflib.unified_diff(old_lines, new_lines,
                                             fromfile=f"a/{path}" if old else "/dev/null",
                                             tofile=f"b/{path}" if new else "/dev/null"):
                emit(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return _text(bytes(output))

    def _batch(self, pages: dict[str, str]) -> dict[str, bytes]:
        if not isinstance(pages, dict) or not 1 <= len(pages) <= self.MAX_BATCH_PAGES:
            raise WikiError("wiki write requires between 1 and 20 pages")
        result = {}
        total = 0
        for path, content in pages.items():
            _path(path)
            if not isinstance(content, str) or len(content) > self.MAX_PAGE_BYTES:
                raise WikiError("wiki page content must be UTF-8 text")
            try:
                encoded = content.encode("utf-8")
            except UnicodeError:
                raise WikiError("wiki page content must be UTF-8 text") from None
            if len(encoded) > self.MAX_PAGE_BYTES:
                raise WikiError("wiki page byte limit exceeded")
            total += len(encoded)
            if total > self.MAX_BATCH_BYTES:
                raise WikiError("wiki batch byte limit exceeded")
            result[path] = encoded
        _compatible_paths(list(result))
        return result

    def _refresh(self) -> str:
        if self._sha is None:
            raise WikiError("wiki must be initialized before writing")
        current = self._fetch()
        if current is None:
            raise WikiError("wiki base conflict; remote branch is missing")
        self._sha = current
        return current

    def _remote_tip(self) -> str:
        self._request()
        result = self._transport.get_refs(self._transport_path, protocol_version=2)
        branch_ref = Ref(("refs/heads/" + self._branch).encode("utf-8"))
        current = result.refs.get(branch_ref)
        if (
            current is None
            or not re.fullmatch(b"[0-9a-f]{40}", current)
            or result.symrefs is None
            or result.symrefs.get(Ref(b"HEAD")) != branch_ref
            or result.refs.get(Ref(b"HEAD")) != current
        ):
            raise WikiError("wiki default branch could not be verified")
        return current.decode("ascii")

    def _updated_tree(self, tree_id: APIObjectID | None, updates: dict[bytes, tuple[int, APIObjectID]]) -> APIObjectID:
        self._check_budget()
        tree = self._get_object(tree_id, Tree) if tree_id is not None else Tree()
        children: dict[bytes, dict[bytes, tuple[int, APIObjectID]]] = {}
        for path, value in updates.items():
            name, separator, rest = path.partition(b"/")
            if separator:
                children.setdefault(name, {})[rest] = value
            else:
                tree.add(name, value[0], value[1])
        for name, nested in children.items():
            previous = tree[name][1] if name in tree else None
            tree.add(name, 0o40000, self._updated_tree(previous, nested))
        self._store.add_object(tree)
        return tree.id

    @_safe
    def write(self, pages: dict[str, str], expected_base: str, message: str, *, author_name: str, author_email: str) -> _WikiWriteResult:
        """Publish one direct child using an internal exact-old-ref CAS lease.

        The lease is never a user option: the new commit's only parent must be
        expected_base. Thus even the leased update is strictly fast-forward.
        Stale retries are no-ops only on descendant snapshots with every requested
        blob already present; rewritten history and unsafe modes remain conflicts.
        """
        batch = self._batch(pages)
        if _ref(expected_base) == "HEAD":
            raise WikiError("expected_base must be a full commit SHA")
        expected_base = expected_base.lower()
        message = _summary(message, "message", 512)
        author_name = _summary(author_name, "author name", 200)
        author_email = _summary(author_email, "author email", 254)
        if any(char in author_name for char in "<>\\") or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\[\]-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", author_email):
            raise WikiError("invalid wiki author identity")
        if ".." in author_email or author_email.startswith(".") or ".@" in author_email:
            raise WikiError("invalid wiki author identity")
        current = self._refresh()
        inventory = self._inventory(current, include_trees=True)
        entries = {path: entry for path, entry in inventory.items() if entry[0] != "040000"}
        directories = {path for path, entry in inventory.items() if entry[0] == "040000"}
        combined = list(set(entries) | set(batch))
        _compatible_paths(combined, directories)
        for path in batch:
            parts = path.split("/")
            directories.update("/".join(parts[:length]) for length in range(1, len(parts)))
        if len(combined) + len(directories) > self.MAX_TREE_ENTRIES:
            raise WikiError("wiki tree entry limit exceeded")
        projected = dict(entries)
        for path in batch:
            projected.setdefault(path, ("100644", "", 0))
        self._pages(projected)
        for path in batch:
            if path in entries and entries[path][0] not in _REGULAR_MODES:
                raise WikiError("wiki write cannot replace a symlink or submodule")
        if expected_base != current:
            if (len(expected_base) != 40 or not any(
                sha.decode("ascii") == expected_base
                for sha, commit in self._ancestors(_object_id(current.encode("ascii")))
            )):
                raise WikiError("wiki base conflict; remote history changed")
            previous_entries = self._inventory(expected_base)
            for path in batch:
                expected_mode = previous_entries[path][0] if path in previous_entries else "100644"
                if path in entries and entries[path][0] != expected_mode:
                    raise WikiError("wiki base conflict; requested page mode changed")
        blobs = {path: Blob.from_string(content) for path, content in batch.items()}
        changed = [path for path, blob in blobs.items() if path not in entries or blob.id.decode("ascii") != entries[path][1]]
        result: _WikiWriteResult = {"status": "no-change", "sha": current, "branch": self._branch, "pages": [], "repository": self.repository}
        if not changed:
            return result
        if expected_base != current:
            raise WikiError("wiki base conflict; requested content differs from the current snapshot")
        updates: dict[bytes, tuple[int, APIObjectID]] = {}
        for path in changed:
            self._store.add_object(blobs[path])
            updates[path.encode("utf-8")] = (int(entries[path][0], 8) if path in entries else 0o100644, blobs[path].id)
        parent = self._get_object(_object_id(current.encode("ascii")), Commit)
        commit = Commit()
        commit.tree = self._updated_tree(parent.tree, updates)
        commit.parents = [parent.id]
        commit.author = commit.committer = f"{author_name} <{author_email}>".encode("utf-8")
        commit.author_time = commit.commit_time = int(time.time())
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = (message + "\n").encode("utf-8")
        self._store.add_object(commit)
        branch_ref = Ref(("refs/heads/" + self._branch).encode("utf-8"))

        def update_refs(remote_refs: dict[Ref, APIObjectID]) -> dict[Ref, APIObjectID]:
            self._check_budget()
            if remote_refs.get(branch_ref) != expected_base.encode("ascii"):
                raise WikiError("wiki base conflict; remote advertisement changed")
            return {branch_ref: commit.id}

        try:
            self._request()
            sent = self._transport.send_pack(self._transport_path, update_refs,
                                             self._store.generate_pack_data, progress=self._progress)
            if sent.ref_status is None or any(status is not None for status in sent.ref_status.values()):
                raise WikiError("wiki receive-pack rejected the update")
            if self._remote_tip() != commit.id.decode("ascii"):
                raise WikiError("wiki remote verification failed")
        except Exception:
            raise WikiError("wiki publish conflict or outcome unknown; re-read a snapshot") from None
        self._sha = commit.id.decode("ascii")
        self._reachable.add(commit.id)
        self._repository.refs[branch_ref] = commit.id
        result["status"] = "updated"
        result["sha"] = self._sha
        result["pages"] = sorted(changed)
        return result
