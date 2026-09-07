"""Bounded, host-side access to the traditional repository wiki.

The class is deliberately not registered as an MCP tool.  It is a trusted
host capability used by a publisher after proposal and authorization checks.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


class WikiError(RuntimeError):
    pass


_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")


def wiki_remote(repository: str) -> str:
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository.strip()):
        raise WikiError("repository must use owner/repository format")
    owner, name = repository.strip().split("/", 1)
    return f"https://github.com/{owner}/{name}.wiki.git"


def _page(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise WikiError("wiki page path is invalid")
    parts = PurePosixPath(path).parts
    if path.startswith("/") or "\\" in path or any(p in {"", ".", "..", ".git"} for p in parts):
        raise WikiError("unsafe wiki page path")
    if not path.casefold().endswith(".md"):
        raise WikiError("generated wiki pages must be Markdown")
    return path


class WikiRepository:
    """Use an isolated, non-interactive clone for bounded wiki reads."""

    def __init__(self, repository: str, *, timeout: int = 30) -> None:
        self.repository = repository
        self.remote = wiki_remote(repository)
        self.timeout = timeout
        self._root: Path | None = None

    def __enter__(self) -> "WikiRepository":
        parent = Path(tempfile.mkdtemp(prefix="issuelens-wiki-"))
        root = parent / "repo"
        self._root = root
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        try:
            result = subprocess.run(
                ["git", "clone", "--no-checkout", "--no-tags", self.remote, str(root)],
                check=True, capture_output=True, text=True, timeout=self.timeout, env=env,
            )
            self._run("config", "core.hooksPath", os.devnull, env=env)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self._root is not None:
            import shutil
            shutil.rmtree(self._root.parent, ignore_errors=True)
            self._root = None

    def _run(self, *args: str, env: dict[str, str] | None = None) -> str:
        if self._root is None:
            raise WikiError("wiki repository is not open")
        try:
            result = subprocess.run(
                ["git", "-C", str(self._root), *args],
                check=True, capture_output=True, text=True, timeout=self.timeout,
                env=env,
            )
        except FileNotFoundError as error:
            raise WikiError("Git is required for wiki access") from error
        except subprocess.CalledProcessError as error:
            raise WikiError("wiki Git operation failed") from error
        except subprocess.TimeoutExpired as error:
            raise WikiError("wiki Git operation timed out") from error
        return result.stdout

    def snapshot(self) -> dict[str, Any]:
        branch = self._run("symbolic-ref", "--short", "HEAD").strip()
        sha = self._run("rev-parse", "HEAD").strip()
        return {"repository": self.repository, "branch": branch, "sha": sha, "initialized": True}

    def pages(self, ref: str = "HEAD") -> list[dict[str, str]]:
        listing = self._run("ls-tree", "-r", "--name-only", ref).splitlines()
        result = []
        for path in listing[:1000]:
            if path.casefold().endswith(".md"):
                result.append({"path": _page(path), "sha": self._run("rev-parse", f"{ref}:{path}").strip()})
        return result

    def page(self, path: str, ref: str = "HEAD") -> dict[str, str]:
        path = _page(path)
        content = self._run("show", f"{ref}:{path}")
        if len(content.encode()) > 256 * 1024:
            raise WikiError("wiki page exceeds bounded size")
        return {"path": path, "ref": ref, "sha": hashlib.sha256(content.encode()).hexdigest(), "content": content}

    def search(self, query: str, ref: str = "HEAD") -> list[dict[str, str]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise WikiError("query is invalid")
        return [item for item in self.pages(ref) if query.casefold() in self.page(item["path"], ref)["content"].casefold()]

    def history(self, path: str | None = None, limit: int = 30) -> list[str]:
        if not 1 <= limit <= 100:
            raise WikiError("limit must be between 1 and 100")
        args = ["log", f"-{limit}", "--format=%H"]
        if path:
            args += ["--", _page(path)]
        return self._run(*args).splitlines()

    def diff(self, base: str, head: str = "HEAD") -> str:
        return self._run("diff", "--no-ext-diff", "--binary", base, head)[:512 * 1024]
