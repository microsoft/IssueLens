"""Bounded wiki Git access without a worktree or ambient Git configuration."""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from .auth import GitHubAppError, validate_repository


class WikiError(RuntimeError):
    """A bounded, credential-free wiki failure."""


_CREATE_SUSPENDED = 0x4


class _ProcessTree:
    """Kill transport descendants as well as Git, including on Windows."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self._lock = threading.Lock()
        self._handle = None
        if os.name != "nt":
            return
        import ctypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", ctypes.c_uint32), ("minimum_working_set", ctypes.c_size_t),
                        ("maximum_working_set", ctypes.c_size_t), ("active_processes", ctypes.c_uint32),
                        ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32), ("scheduling", ctypes.c_uint32)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self._kernel.SetInformationJobObject.restype = ctypes.c_int
        self._kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._kernel.AssignProcessToJobObject.restype = ctypes.c_int
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel.CloseHandle.restype = ctypes.c_int
        handle = self._kernel.CreateJobObjectW(None, None)
        if not handle:
            raise WikiError("wiki process isolation is unavailable")
        self._handle = handle
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000
            if (not self._kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                    or not self._kernel.AssignProcessToJobObject(handle, int(process._handle))):
                raise WikiError("wiki process isolation is unavailable")
            self._resume_primary_thread()
        except BaseException:
            self.close()
            raise

    def _resume_primary_thread(self) -> None:
        import ctypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                        ("th32ThreadID", ctypes.c_uint32), ("th32OwnerProcessID", ctypes.c_uint32),
                        ("tpBasePri", ctypes.c_int32), ("tpDeltaPri", ctypes.c_int32),
                        ("dwFlags", ctypes.c_uint32)]

        self._kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self._kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        self._kernel.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry)]
        self._kernel.Thread32First.restype = ctypes.c_int
        self._kernel.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry)]
        self._kernel.Thread32Next.restype = ctypes.c_int
        self._kernel.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self._kernel.OpenThread.restype = ctypes.c_void_p
        self._kernel.ResumeThread.argtypes = [ctypes.c_void_p]
        self._kernel.ResumeThread.restype = ctypes.c_uint32
        snapshot = self._kernel.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise WikiError("wiki process thread snapshot is unavailable")
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = self._kernel.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if (entry.dwSize >= ThreadEntry.th32OwnerProcessID.offset + ctypes.sizeof(ctypes.c_uint32)
                        and entry.th32OwnerProcessID == self.process.pid):
                    thread = self._kernel.OpenThread(0x2, False, entry.th32ThreadID)
                    if not thread:
                        raise WikiError("wiki process thread could not be opened")
                    try:
                        if self._kernel.ResumeThread(thread) != 1:
                            raise WikiError("wiki process thread could not be resumed")
                    finally:
                        self._kernel.CloseHandle(thread)
                    return
                entry.dwSize = ctypes.sizeof(entry)
                present = self._kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise WikiError("wiki process primary thread is unavailable")
        finally:
            self._kernel.CloseHandle(snapshot)

    def close(self) -> None:
        with self._lock:
            if os.name == "nt":
                if self._handle is not None:
                    self._kernel.CloseHandle(self._handle)
                    self._handle = None
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_REGULAR_MODES = {"100644", "100755"}
_RESERVED = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9\u00b9\u00b2\u00b3]|lpt[1-9\u00b9\u00b2\u00b3])(?:\.|$)", re.I)


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


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeError:
        raise WikiError("wiki content must be UTF-8") from None


def _compatible_paths(paths: list[str]) -> None:
    components: dict[str, tuple[str, bool]] = {}
    for path in paths:
        _path(path, markdown=False)
        parts = path.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = unicodedata.normalize("NFC", prefix).casefold()
            is_file = length == len(parts)
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


class WikiRepository:
    """A snapshot-pinned bare clone with a context-wide deadline and budgets.

    Only context entry and write may contact the derived HTTPS remote. Full
    default-branch history is cloned within the disk/time limits; unavailable
    commits fail explicitly. Empty repositories can be inspected but not written.
    """

    MAX_DISK_BYTES = 64 * 1024 * 1024
    MAX_DISK_ENTRIES = 20_000
    MAX_OUTPUT_BYTES = 512 * 1024
    MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
    MAX_COMMANDS = 512
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
        self._root: Path | None = None
        self._env: dict[str, str] = {}
        self._sha: str | None = None
        self._branch = ""
        self._deadline = 0.0
        self._output_bytes = 0
        self._commands = 0

    @property
    def remote(self) -> str:
        return f"https://github.com/{self.repository}.wiki.git"

    def _environment(self, parent: Path) -> dict[str, str]:
        essentials = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "PATHEXT"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in essentials}
        environment.update({
            "HOME": str(parent), "USERPROFILE": str(parent), "XDG_CONFIG_HOME": str(parent),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never", "GIT_ATTR_NOSYSTEM": "1",
            "GIT_LITERAL_PATHSPECS": "1", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_INDEX_FILE": str(parent / "index"),
            "GIT_ALLOW_PROTOCOL": "https", "LC_ALL": "C", "LANG": "C",
        })
        settings = [
            ("credential.helper", ""), ("credential.interactive", "false"),
            ("core.askPass", ""), ("core.hooksPath", str(parent / "hooks")),
            ("init.templateDir", str(parent / "templates")),
            ("http.followRedirects", "false"), ("http.sslVerify", "true"),
            ("submodule.recurse", "false"), ("fetch.recurseSubmodules", "false"),
            ("core.fsmonitor", "false"), ("core.autocrlf", "false"),
            ("core.attributesFile", os.devnull), ("core.excludesFile", os.devnull),
            ("core.quotePath", "false"), ("commit.gpgSign", "false"),
            ("core.protectNTFS", "true"), ("core.protectHFS", "true"),
            ("tag.gpgSign", "false"), ("push.gpgSign", "false"),
            ("gc.auto", "0"), ("maintenance.auto", "false"),
            ("pack.threads", "1"), ("pack.windowMemory", "8m"),
            ("http.maxRequests", "1"), ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
        ]
        if self._token:
            credential = base64.b64encode(f"x-access-token:{self._token}".encode("ascii")).decode("ascii")
            settings.append(("http.https://github.com/.extraHeader", f"Authorization: Basic {credential}"))
        environment["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        return environment

    def __enter__(self) -> WikiRepository:
        if self._parent is not None:
            raise WikiError("wiki repository is already open")
        self._deadline = time.monotonic() + self.timeout
        self._commands = self._output_bytes = 0
        try:
            self._parent = Path(tempfile.mkdtemp(prefix="issuelens-wiki-")).resolve()
        except OSError:
            raise WikiError("wiki temporary storage could not be created") from None
        self._root = self._parent / "repo.git"
        self._env = self._environment(self._parent)
        try:
            (self._parent / "hooks").mkdir()
            (self._parent / "templates").mkdir()
            self._run("clone", "--bare", "--single-branch", "--no-tags", "--no-local", "--no-hardlinks", "--", self.remote, str(self._root), external=True)
            branch_ref = _text(self._run("symbolic-ref", "HEAD")).strip()
            if not branch_ref.startswith("refs/heads/"):
                raise WikiError("wiki default branch is unavailable")
            self._branch = branch_ref[len("refs/heads/"):]
            self._run("check-ref-format", branch_ref)
            if not self._branch or len(self._branch) > 200 or any(ord(char) < 33 or ord(char) > 126 for char in self._branch):
                raise WikiError("invalid wiki default branch")
            present = self._run("show-ref", "--heads", allowed_codes=(0, 1))
            self._sha = self._resolve("HEAD", pinned=False) if present else None
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self._parent is None:
            return

        def remove_readonly(function: Any, path: str, error: Any) -> None:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        cleanup_deadline = time.monotonic() + 1.0
        retry = threading.Event()
        try:
            while True:
                try:
                    shutil.rmtree(self._parent, onerror=remove_readonly)
                    self._parent = None
                    break
                except OSError:
                    if not self._parent.exists():
                        self._parent = None
                        break
                    if os.name != "nt" or time.monotonic() >= cleanup_deadline:
                        raise WikiError("wiki temporary storage cleanup failed") from None
                    retry.wait(0.025)
        finally:
            self._root = None
            self._env.clear()
            self._sha = None

    def _check_budget(self) -> None:
        if self._parent is None:
            raise WikiError("wiki repository is not open")
        if time.monotonic() >= self._deadline:
            raise WikiError("wiki operation exceeded time budget")
        total = count = 0
        pending = [self._parent]
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    count += 1
                    if entry.is_symlink():
                        raise WikiError("unexpected link in wiki temporary storage")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    else:
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except FileNotFoundError:
                            continue
                    if total > self.MAX_DISK_BYTES or count > self.MAX_DISK_ENTRIES:
                        raise WikiError("wiki temporary storage budget exceeded")
                    if count % 64 == 0 and time.monotonic() >= self._deadline:
                        raise WikiError("wiki operation exceeded time budget")

    def _run(self, *arguments: str, input_bytes: bytes | None = None, max_output: int | None = None, external: bool = False, allowed_codes: tuple[int, ...] = (0,)) -> bytes:
        self._check_budget()
        if input_bytes is not None and len(input_bytes) > self.MAX_BATCH_BYTES:
            raise WikiError("wiki Git input budget exceeded")
        self._commands += 1
        if self._commands > self.MAX_COMMANDS:
            raise WikiError("wiki command budget exceeded")
        limit = min(self.MAX_OUTPUT_BYTES, max_output if max_output is not None else self.MAX_OUTPUT_BYTES)
        command = ["git", *([] if external else ["-C", str(self._root)]), *arguments]
        buffers = [bytearray(), bytearray()]
        failure: list[str] = []
        finished = threading.Event()
        lock = threading.Lock()
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd=self._parent, env=self._env, shell=False, start_new_session=os.name != "nt",
                                       creationflags=_CREATE_SUSPENDED if os.name == "nt" else 0)
        except OSError:
            raise WikiError("Git is unavailable for wiki access") from None
        try:
            process_tree = _ProcessTree(process)
        except BaseException:
            try:
                process.kill()
                process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            raise

        def stop(reason: str) -> None:
            with lock:
                if not failure:
                    failure.append(reason)
            try:
                process_tree.close()
                process.kill()
            except (OSError, ProcessLookupError):
                pass

        def read_stream(stream: Any, index: int, cap: int) -> None:
            try:
                while chunk := stream.read1(8192):
                    with lock:
                        remaining = max(0, cap - len(buffers[index]))
                        buffers[index].extend(chunk[:remaining])
                        self._output_bytes += len(chunk)
                        exceeded = len(chunk) > remaining or self._output_bytes > self.MAX_TOTAL_OUTPUT_BYTES
                    if exceeded:
                        stop("wiki Git output budget exceeded")
                        break
            except OSError:
                stop("wiki Git output could not be read")

        def feed() -> None:
            try:
                if input_bytes:
                    process.stdin.write(input_bytes)
                process.stdin.close()
            except (OSError, BrokenPipeError):
                stop("wiki Git input failed")

        def watch() -> None:
            while not finished.wait(0.02):
                try:
                    self._check_budget()
                except (WikiError, OSError) as error:
                    stop(str(error) if isinstance(error, WikiError) else "wiki storage could not be monitored")
                    return
        workers = [threading.Thread(target=read_stream, args=(process.stdout, 0, limit)),
                   threading.Thread(target=read_stream, args=(process.stderr, 1, 8192)),
                   threading.Thread(target=feed), threading.Thread(target=watch)]
        try:
            for worker in workers:
                worker.start()
            process.wait(timeout=max(0.001, self._deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stop("wiki operation exceeded time budget")
            process.wait()
        except BaseException:
            stop("wiki Git operation interrupted")
            process.wait()
            raise
        finally:
            process_tree.close()
            finished.set()
            for worker in workers:
                worker.join()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        if failure:
            raise WikiError(failure[0])
        self._check_budget()
        if process.returncode not in allowed_codes:
            raise WikiError("wiki Git operation failed")
        return bytes(buffers[0])

    def _resolve(self, ref: str, *, pinned: bool = True) -> str:
        ref = _ref(ref)
        if ref != "HEAD":
            ref = ref.lower()
        if pinned and ref == "HEAD":
            if self._sha is None:
                raise WikiError("wiki has no initialized snapshot")
            ref = self._sha
        try:
            resolved = _text(self._run("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", max_output=128)).strip()
        except WikiError:
            raise WikiError("wiki commit unavailable or read budget exceeded; re-read a snapshot") from None
        if not _SHA.fullmatch(resolved):
            raise WikiError("wiki commit unavailable")
        return resolved

    def snapshot(self) -> dict[str, Any]:
        self._check_budget()
        return {"repository": self.repository, "branch": self._branch, "sha": self._sha, "initialized": self._sha is not None}

    def _inventory(self, sha: str) -> dict[str, tuple[str, str, int]]:
        raw = self._run("ls-tree", "-r", "-z", "-l", sha)
        records = raw.split(b"\0")
        if len(records) - 1 > self.MAX_TREE_ENTRIES:
            raise WikiError("wiki tree entry limit exceeded")
        result: dict[str, tuple[str, str, int]] = {}
        for record in records:
            if not record:
                continue
            metadata, path_bytes = record.split(b"\t", 1)
            mode, kind, oid, size = metadata.split()
            path = _text(path_bytes)
            result[path] = (_text(mode), _text(oid), int(size) if kind == b"blob" else -1)
        _compatible_paths(list(result))
        return result

    def _pages(self, entries: dict[str, tuple[str, str, int]]) -> list[dict[str, str]]:
        result = [{"path": _path(path), "sha": oid} for path, (mode, oid, _) in entries.items()
                  if path.casefold().endswith(".md") and mode in _REGULAR_MODES]
        if len(result) > self.MAX_PAGES:
            raise WikiError("wiki page count limit exceeded")
        return result

    def pages(self, ref: str = "HEAD") -> list[dict[str, str]]:
        return self._pages(self._inventory(self._resolve(ref)))

    def page(self, path: str, ref: str = "HEAD") -> dict[str, str]:
        path = _path(path)
        sha = self._resolve(ref)
        entry = self._inventory(sha).get(path)
        if entry is None or entry[0] not in _REGULAR_MODES:
            raise WikiError("wiki page is missing or is not a regular file")
        if entry[2] > self.MAX_PAGE_BYTES:
            raise WikiError("wiki page byte limit exceeded")
        content = _text(self._run("cat-file", "blob", entry[1], max_output=self.MAX_PAGE_BYTES))
        return {"path": path, "ref": sha, "sha": entry[1], "content": content}

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
            content = _text(self._run("cat-file", "blob", page["sha"], max_output=self.MAX_PAGE_BYTES))
            if query.casefold() in content.casefold():
                result.append(page)
        return result

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
        arguments = ["log", "--no-show-signature", "--format=%H", f"--max-count={limit}", sha, "--"]
        if path is not None:
            arguments.append(path)
        return _text(self._run(*arguments, max_output=6600)).splitlines()

    def diff(self, base: str, head: str = "HEAD") -> str:
        _ref(base)
        _ref(head)
        base_sha, head_sha = self._resolve(base), self._resolve(head)
        return _text(self._run("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--binary", "--no-renames", base_sha, head_sha, "--"))

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
        branch_ref = f"refs/heads/{self._branch}"
        self._run("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--", self.remote,
                  f"{branch_ref}:refs/issuelens/current")
        current = _text(self._run("show-ref", "--verify", "--hash", "refs/issuelens/current", max_output=128)).strip()
        self._sha = self._resolve(current)
        return self._sha

    def _remote_tip(self) -> str:
        branch_ref = f"refs/heads/{self._branch}"
        output = self._run("ls-remote", "--exit-code", "--refs", "--", self.remote, branch_ref, max_output=1024)
        rows = _text(output).splitlines()
        if len(rows) != 1:
            raise WikiError("wiki remote branch could not be verified")
        parts = rows[0].split("\t")
        if len(parts) != 2 or not _SHA.fullmatch(parts[0]) or parts[1] != branch_ref:
            raise WikiError("wiki remote branch could not be verified")
        return parts[0]

    def write(self, pages: dict[str, str], expected_base: str, message: str, *, author_name: str, author_email: str) -> dict[str, Any]:
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
        entries = self._inventory(current)
        combined = list(set(entries) | set(batch))
        _compatible_paths(combined)
        if len(combined) > self.MAX_TREE_ENTRIES:
            raise WikiError("wiki tree entry limit exceeded")
        projected = dict(entries)
        for path in batch:
            projected.setdefault(path, ("100644", "", 0))
        self._pages(projected)
        for path in batch:
            if path in entries and entries[path][0] not in _REGULAR_MODES:
                raise WikiError("wiki write cannot replace a symlink or submodule")
        if expected_base != current:
            try:
                expected = self._resolve(expected_base)
                ancestor = _text(self._run("merge-base", expected, current, max_output=128)).strip()
            except WikiError:
                raise WikiError("wiki base conflict; re-read a snapshot") from None
            if ancestor != expected_base:
                raise WikiError("wiki base conflict; remote history changed")
            previous_entries = self._inventory(expected_base)
            for path in batch:
                expected_mode = previous_entries[path][0] if path in previous_entries else "100644"
                if path in entries and entries[path][0] != expected_mode:
                    raise WikiError("wiki base conflict; requested page mode changed")
        changed = []
        for path, content in batch.items():
            blob = f"blob {len(content)}\0".encode("ascii") + content
            oid = hashlib.sha256(blob).hexdigest() if len(current) == 64 else hashlib.sha1(blob, usedforsecurity=False).hexdigest()
            if path not in entries or oid != entries[path][1]:
                changed.append(path)
        result = {"status": "no-change", "sha": current, "branch": self._branch, "pages": [], "repository": self.repository}
        if not changed:
            return result
        if expected_base != current:
            raise WikiError("wiki base conflict; requested content differs from the current snapshot")
        self._run("read-tree", current)
        index_entries = bytearray()
        for path in sorted(changed):
            oid = _text(self._run("hash-object", "-w", "--no-filters", "--stdin", input_bytes=batch[path], max_output=128)).strip()
            if not _SHA.fullmatch(oid):
                raise WikiError("wiki blob could not be verified")
            mode = entries[path][0] if path in entries else "100644"
            index_entries.extend(f"{mode} {oid}\t{path}\0".encode("utf-8"))
        self._run("update-index", "-z", "--index-info", input_bytes=bytes(index_entries))
        tree = _text(self._run("write-tree", max_output=128)).strip()
        if not _SHA.fullmatch(tree):
            raise WikiError("wiki tree could not be verified")
        identity = {"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
                    "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email}
        self._env.update(identity)
        try:
            new_sha = _text(self._run("commit-tree", tree, "-p", current, input_bytes=(message + "\n").encode("utf-8"), max_output=128)).strip()
        finally:
            for key in identity:
                self._env.pop(key, None)
        if not _SHA.fullmatch(new_sha):
            raise WikiError("wiki commit could not be verified")
        parents = _text(self._run("rev-list", "--parents", "--max-count=1", new_sha, "--", max_output=256)).split()
        if parents != [new_sha, expected_base]:
            raise WikiError("wiki publication must be a direct fast-forward child")
        actual_changes = _text(self._run("diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", current, new_sha, "--")).split("\0")
        if sorted(filter(None, actual_changes)) != sorted(changed):
            raise WikiError("wiki commit changed unexpected paths")
        branch_ref = f"refs/heads/{self._branch}"
        try:
            self._run("push", "--porcelain", "--atomic", f"--force-with-lease={branch_ref}:{expected_base}", "--", self.remote, f"{new_sha}:{branch_ref}")
            if self._remote_tip() != new_sha:
                raise WikiError("wiki remote verification failed")
        except WikiError:
            raise WikiError("wiki publish conflict or outcome unknown; re-read a snapshot") from None
        self._sha = new_sha
        result.update(status="updated", sha=new_sha, pages=sorted(changed))
        return result
