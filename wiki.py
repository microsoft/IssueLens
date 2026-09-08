"""Bounded, host-side access to the traditional repository wiki.

The class is deliberately not registered as an MCP tool.  It is a trusted
host capability used by a publisher after proposal and authorization checks.
"""

from __future__ import annotations

import hashlib
import base64
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

    def __init__(self, repository: str, *, token: str | None = None, timeout: int = 30) -> None:
        self.repository = repository
        self.remote = wiki_remote(repository)
        self.timeout = timeout
        self._token = token
        self._root: Path | None = None

    def __enter__(self) -> "WikiRepository":
        parent = Path(tempfile.mkdtemp(prefix="issuelens-wiki-"))
        root = parent / "repo"
        self._root = root
        env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1" if self._token else "0",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": (
                "Authorization: Basic "
                + base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
                if self._token else ""
            ),
        }
        try:
            self._run_external(
                ["clone", "--depth", "100", "--no-checkout", "--no-tags",
                 self.remote, str(root)],
                env=env,
            )
            default_ref = self._run("symbolic-ref", "refs/remotes/origin/HEAD").strip()
            default_branch = self._ref(default_ref.rsplit("/", 1)[-1])
            self._run("checkout", "-B", default_branch, f"origin/{default_branch}", env=env)
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

    def _ref(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref or len(ref) > 200:
            raise WikiError("wiki ref is invalid")
        if any(part in ref for part in ("\x00", "..", "\\", "?", "#", " ")):
            raise WikiError("wiki ref contains unsupported characters")
        return ref

    def _run_external(self, args: list[str], *, env: dict[str, str]) -> str:
        try:
            result = subprocess.run(
                ["git", *args], check=True, capture_output=True, text=True,
                timeout=self.timeout, env=env,
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
        ref = self._ref(ref)
        listing = self._run("ls-tree", "-r", "--name-only", ref).splitlines()
        result = []
        for path in listing[:1000]:
            if path.casefold().endswith(".md"):
                result.append({"path": _page(path), "sha": self._run("rev-parse", f"{ref}:{path}").strip()})
        return result

    def page(self, path: str, ref: str = "HEAD") -> dict[str, str]:
        path = _page(path)
        ref = self._ref(ref)
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
        return self._run("diff", "--no-ext-diff", "--binary", self._ref(base), self._ref(head))[:512 * 1024]

    def publish(self, pages: dict[str, str], expected_base: str, author: str) -> dict[str, Any]:
        snapshot = self.snapshot()
        if snapshot["sha"] != expected_base:
            raise WikiError("wiki base changed; proposal is stale")
        for path, content in pages.items():
            target = (self._root / _page(path)).resolve()
            if self._root not in target.parents:
                raise WikiError("wiki page escapes repository")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        changed = self._run("status", "--porcelain", "--", *(_page(p) for p in pages)).strip()
        if not changed:
            return {"status": "no-change", "sha": snapshot["sha"], "pages": []}
        self._run("config", "user.name", author)
        self._run("config", "user.email", "issue-lens-app[bot]@users.noreply.github.com")
        self._run("add", "--", *(_page(p) for p in pages))
        self._run("commit", "-m", "Update team memory")
        new_sha = self._run("rev-parse", "HEAD").strip()
        env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": (
                "Authorization: Basic "
                + base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            ),
        }
        self._run("push", "--porcelain", "origin", f"HEAD:{snapshot['branch']}", env=env)
        verified = self._run("rev-parse", "HEAD").strip()
        if verified != new_sha:
            raise WikiError("published wiki revision could not be verified")
        return {"status": "updated", "sha": new_sha, "pages": sorted(pages)}
