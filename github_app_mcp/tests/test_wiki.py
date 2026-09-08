from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from issuelens_github_mcp.wiki import WikiError, WikiRepository, _ProcessTree


class WikiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wiki-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.remote = self.directory / "fixture.git"
        self.seed = self.directory / "seed"
        self.seed.mkdir()
        self.environment = WikiRepository("example/repository")._environment(self.directory)
        self.environment["GIT_ALLOW_PROTOCOL"] = "file"
        self.environment.pop("GIT_INDEX_FILE")
        self.environment.update({"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
                                 "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test"})
        self.git(self.seed, "init", "--initial-branch=docs/wiki")
        (self.seed / "Home.md").write_bytes(b"# Home\nWelcome\n")
        (self.seed / "images").mkdir()
        (self.seed / "images" / "logo.bin").write_bytes(bytes(range(256)))
        self.unicode_path = "notes/\u8bbe\u8ba1-\u00e9.md"
        (self.seed / "notes").mkdir()
        (self.seed / self.unicode_path).write_bytes("# \u8bbe\u8ba1\nCaf\u00e9\n".encode("utf-8"))
        self.git(self.seed, "add", "--all")
        self.git(self.seed, "commit", "-m", "Seed")
        self.base = self.git(self.seed, "rev-parse", "HEAD").decode().strip()
        self.git(self.directory, "clone", "--bare", "--", str(self.seed), str(self.remote))
        fixture_remote = self.remote
        class LocalWiki(WikiRepository):
            @property
            def remote(self) -> str:
                return str(fixture_remote)

            def _environment(self, parent: Path) -> dict[str, str]:
                environment = super()._environment(parent)
                environment["GIT_ALLOW_PROTOCOL"] = "file"
                return environment
        self.local_wiki = LocalWiki

    def git(self, directory: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
        return subprocess.run(["git", "-C", str(directory), *arguments], input=input_bytes,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                              timeout=15, env=self.environment, shell=False).stdout

    def test_bare_snapshot_and_unicode_reads(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            self.assertEqual(wiki.snapshot(), {"repository": "example/repository", "branch": "docs/wiki",
                                              "sha": self.base, "initialized": True})
            self.assertEqual([page["path"] for page in wiki.pages()], ["Home.md", self.unicode_path])
            page = wiki.page(self.unicode_path)
            self.assertEqual(page["content"], "# \u8bbe\u8ba1\nCaf\u00e9\n")
            self.assertEqual(page["ref"], self.base)
            self.assertEqual(page["sha"], self.git(self.seed, "rev-parse", f"HEAD:{self.unicode_path}").decode().strip())
            self.assertFalse((wiki._root / "Home.md").exists())
            parent = wiki._parent
        self.assertFalse(parent.exists())

    def write(self, wiki: WikiRepository, pages: dict[str, str], base: str | None = None) -> dict:
        return wiki.write(pages, base or self.base, "Update team notes", author_name="IssueLens App",
                          author_email="123+issuelens[bot]@users.noreply.github.com")

    def seed_entries(self, entries: dict[str, tuple[str, bytes]]) -> str:
        for path, (mode, content) in entries.items():
            oid = self.base if mode == "160000" else self.git(self.seed, "hash-object", "-w", "--stdin", input_bytes=content).decode().strip()
            self.git(self.seed, "update-index", "-z", "--index-info", input_bytes=f"{mode} {oid}\t{path}\0".encode("utf-8"))
        self.git(self.seed, "commit", "-m", "Fixture entries")
        self.git(self.seed, "push", "--", str(self.remote), "HEAD:refs/heads/docs/wiki")
        return self.git(self.seed, "rev-parse", "HEAD").decode().strip()

    def test_atomic_write_preserves_content_and_supports_reads(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            old_home = wiki.page("Home.md")
            old_asset = self.git(self.remote, "rev-parse", "HEAD:images/logo.bin")
            updated = self.write(wiki, {self.unicode_path: "# Revised\nCaf\u00e9\n", "New.md": "New page\n"})
            self.assertEqual(set(updated), {"status", "sha", "branch", "pages", "repository"})
            self.assertEqual(updated["status"], "updated")
            self.assertEqual(updated["branch"], "docs/wiki")
            self.assertEqual(wiki.page("Home.md")["sha"], old_home["sha"])
            self.assertEqual(self.git(self.remote, "rev-parse", "HEAD:images/logo.bin"), old_asset)
            self.assertEqual(self.git(self.remote, "rev-parse", "HEAD^").decode().strip(), self.base)
            self.assertEqual(self.git(self.remote, "rev-list", "--count", f"{self.base}..HEAD").strip(), b"1")
            self.assertEqual(wiki.page(self.unicode_path, self.base)["content"], "# \u8bbe\u8ba1\nCaf\u00e9\n")
            self.assertEqual(wiki.history(ref=self.base), [self.base])
            self.assertEqual(wiki.history(self.unicode_path), [updated["sha"], self.base])
            self.assertEqual(wiki.history(limit=1), [updated["sha"]])
            self.assertEqual([page["path"] for page in wiki.search("CAF\u00c9")], [self.unicode_path])
            self.assertIn("+# Revised", wiki.diff(self.base))
            self.assertEqual(wiki.diff(updated["sha"]), "")
            self.assertIn(b"IssueLens App <123+issuelens[bot]@users.noreply.github.com>", self.git(self.remote, "cat-file", "commit", updated["sha"]))

    def test_noop_and_stale_retry(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            initial = self.write(wiki, {"Home.md": "# Home\nWelcome\n"})
            self.assertEqual(initial, {"status": "no-change", "sha": self.base, "branch": "docs/wiki", "pages": [], "repository": "example/repository"})
            updated = self.write(wiki, {"Home.md": "New home\n"})
            retry = self.write(wiki, {"Home.md": "New home\n"})
            self.assertEqual(retry["status"], "no-change")
            self.assertEqual(retry["sha"], updated["sha"])
            with self.assertRaisesRegex(WikiError, "conflict"):
                self.write(wiki, {"Home.md": "Different\n"})

    def test_concurrent_update_keeps_reads_pinned_and_rejects_write(self) -> None:
        with self.local_wiki("example/repository") as first, self.local_wiki("example/repository") as second:
            updated = self.write(first, {"Home.md": "First writer\n"})
            self.assertEqual(second.snapshot()["sha"], self.base)
            self.assertEqual(second.page("Home.md")["content"], "# Home\nWelcome\n")
            with self.assertRaisesRegex(WikiError, "conflict"):
                self.write(second, {"Home.md": "Second writer\n"})
            self.assertEqual(self.git(self.remote, "rev-parse", "HEAD").decode().strip(), updated["sha"])

    def test_invalid_batch_and_ref_do_not_change_anything(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            for unsafe in ("../escape.md", "/absolute.md", "-page.md", ".GiT/config.md", "a\\b.md", "C:drive.md", "AUX.md", "line\n.md"):
                with self.subTest(path=unsafe), self.assertRaises(WikiError):
                    self.write(wiki, {"Valid.md": "valid", unsafe: "invalid"})
            outside = self.directory / "outside.txt"
            outside.write_bytes(b"unchanged")
            with self.assertRaises(WikiError):
                wiki.diff(f"--output={outside}")
            self.assertEqual(outside.read_bytes(), b"unchanged")
            self.assertEqual(self.git(self.remote, "rev-parse", "HEAD").decode().strip(), self.base)
            self.assertNotIn("Valid.md", [page["path"] for page in wiki.pages()])


    def test_all_ref_inputs_reject_options_and_unknown_commits(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            for target in (self.directory / "existing.txt", self.directory / "absent.txt"):
                if target.name == "existing.txt":
                    target.write_bytes(b"untouched")
                unsafe = f"--output={target}"
                calls = [lambda: wiki.pages(unsafe), lambda: wiki.page("Home.md", unsafe),
                         lambda: wiki.search("home", unsafe), lambda: wiki.history(ref=unsafe),
                         lambda: wiki.diff(unsafe), lambda: wiki.diff(self.base, unsafe),
                         lambda: self.write(wiki, {"Home.md": "changed"}, unsafe)]
                for call in calls:
                    with self.assertRaises(WikiError):
                        call()
                self.assertEqual(target.read_bytes() if target.exists() else None, b"untouched" if target.name == "existing.txt" else None)
            for invalid in ("main", "HEAD~1", "HEAD:Home.md", self.base[:12], "0" * 40):
                with self.subTest(ref=invalid), self.assertRaises(WikiError):
                    wiki.page("Home.md", invalid)

    def test_symlinks_submodules_and_ancestor_collisions_are_not_pages(self) -> None:
        base = self.seed_entries({"link.md": ("120000", b"Home.md"), "folder": ("120000", b"notes"),
                                  "module.md": ("160000", b""), "submodule": ("160000", b"")})
        with self.local_wiki("example/repository") as wiki:
            self.assertNotIn("link.md", [page["path"] for page in wiki.pages()])
            self.assertNotIn("module.md", [page["path"] for page in wiki.pages()])
            for path in ("link.md", "module.md", "folder/child.md", "submodule/child.md", "images/logo.bin/child.md"):
                with self.subTest(path=path):
                    with self.assertRaises(WikiError):
                        wiki.page(path)
                    with self.assertRaises(WikiError):
                        self.write(wiki, {"Valid.md": "valid", path: "unsafe"}, base)
            self.assertEqual(self.git(self.remote, "rev-parse", "HEAD").decode().strip(), base)
            result = self.write(wiki, {"Valid.md": "safe"}, base)
            self.assertEqual(self.git(self.remote, "rev-parse", f"{result['sha']}:link.md"),
                             self.git(self.remote, "rev-parse", f"{base}:link.md"))

    def test_portable_paths_case_and_unicode_collisions(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            unsafe_paths = ["a//b.md", "a/./b.md", "a/../b.md", "x/.git/a.md", "x/.GIT/a.md",
                            "x/git~1/a.md", "NUL.md", "COM1.md", "lpt9.md", "COM\u00b9.md", "a./b.md",
                            "a /b.md", "a\x00.md", "a\r.md", "a\x7f.md", "a\u200b.md", "a:b.md",
                            "a?.md", "a*.md", "a|b.md", "a<b.md", "a>b.md", 'a"b.md', "a/-x.md",
                            "a.txt", "\ud800.md", "\u8bbe" * 81 + ".md"]
            for path in unsafe_paths:
                with self.subTest(path=ascii(path)), self.assertRaises(WikiError):
                    self.write(wiki, {path: "bad"})
            for batch in ({"home.md": "case"}, {"NOTES/new.md": "directory case"},
                          {"a.md": "a", "A.md": "b"}, {"Caf\u00e9.md": "a", "Cafe\u0301.md": "b"},
                          {"a.md": "a", "a.md/b.md": "b"}):
                with self.assertRaises(WikiError):
                    self.write(wiki, batch)

    def test_entire_batch_and_identity_validated_before_fetch(self) -> None:
        with self.local_wiki("example/repository") as wiki, patch.object(wiki, "_refresh") as refresh:
            invalid_batches = [{}, {f"{index}.md": "a" for index in range(21)},
                               {"valid.md": "ok", "too-big.md": "a" * (64 * 1024 + 1)},
                               {f"{index}.md": "a" * (64 * 1024) for index in range(5)},
                               {"utf8.md": "\u00e9" * (32 * 1024 + 1)}, {"bad.md": "\ud800"},
                               {"bad.md": None}, {None: "bad"}]
            for batch in invalid_batches:
                with self.subTest(batch_keys=list(batch)), self.assertRaises(WikiError):
                    self.write(wiki, batch)
            for overrides in ({"author_name": "Bot\nForged"}, {"author_name": "Bot <evil>"},
                              {"author_name": "Bot\\escape"}, {"author_email": "a@example.test\nBcc:evil"},
                              {"author_email": "a\\b@example.test"}, {"author_email": "a>b@example.test"},
                              {"message": "subject\nextra"}, {"message": "\u00e9" * 257}):
                keywords = {"author_name": "Bot", "author_email": "bot@example.test", "message": "Summary"}
                keywords.update(overrides)
                with self.assertRaises(WikiError):
                    wiki.write({"Home.md": "Home"}, self.base, **keywords)
            refresh.assert_not_called()

    def test_exact_utf8_page_and_batch_limits(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            pages = {f"Page-{index}.md": "\u00e9" * (32 * 1024) for index in range(4)}
            result = self.write(wiki, pages)
            self.assertEqual(result["status"], "updated")
            self.assertEqual(wiki.page("Page-0.md")["content"], pages["Page-0.md"])
            with self.assertRaisesRegex(WikiError, "search byte"):
                wiki.search("anything")

    def test_ambient_git_environment_and_attributes_are_inert(self) -> None:
        self.base = self.seed_entries({".gitattributes": ("100644", b"*.md filter=hostile diff=hostile\n")})
        hostile_config = self.directory / "hostile.config"
        hostile_config.write_text('[filter "hostile"]\n clean = invalid-issuelens-command\n required = true\n[diff "hostile"]\n command = invalid-issuelens-command\n', encoding="utf-8")
        hostile = {"GIT_DIR": str(self.directory / "missing.git"), "GIT_WORK_TREE": str(self.seed),
                   "GIT_INDEX_FILE": str(self.directory / "outside-index"), "GIT_CONFIG_GLOBAL": str(hostile_config),
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "include.path", "GIT_CONFIG_VALUE_0": str(hostile_config),
                   "GIT_EXEC_PATH": str(self.directory), "GIT_SSH_COMMAND": "invalid-command", "SSH_ASKPASS": "invalid-command",
                   "GIT_ASKPASS": "invalid-command", "GCM_CREDENTIAL_STORE": "plaintext", "GIT_TRACE": str(self.directory / "trace")}
        with patch.dict(os.environ, hostile), self.local_wiki("example/repository") as wiki:
            self.assertEqual(wiki.page("Home.md")["ref"], self.base)
            result = self.write(wiki, {"Home.md": "Unfiltered\r\n\u00e9\r\n"})
            self.assertEqual(wiki.page("Home.md")["content"], "Unfiltered\r\n\u00e9\r\n")
            self.assertIn("Unfiltered", wiki.diff(self.base, result["sha"]))
            self.assertFalse((self.directory / "outside-index").exists())
            self.assertFalse((self.directory / "trace").exists())

    def test_inventory_limit_counts_assets_before_filtering(self) -> None:
        self.seed_entries({f"asset-{index}.bin": ("100644", b"asset") for index in range(4)})
        with self.local_wiki("example/repository") as wiki:
            wiki.MAX_TREE_ENTRIES = 5
            with self.assertRaisesRegex(WikiError, "tree entry"):
                wiki.pages()
            wiki.MAX_TREE_ENTRIES = 2048
            wiki.MAX_PAGES = 1
            with self.assertRaisesRegex(WikiError, "page count"):
                wiki.pages()
            with self.assertRaisesRegex(WikiError, "page count"):
                wiki.search("Home")

    def test_large_and_non_utf8_pages_fail_explicitly(self) -> None:
        self.seed_entries({"Large.md": ("100644", b"a" * (64 * 1024 + 1)), "Invalid.md": ("100644", b"\xff\xfe")})
        with self.local_wiki("example/repository") as wiki:
            with self.assertRaisesRegex(WikiError, "page byte"):
                wiki.page("Large.md")
            with self.assertRaisesRegex(WikiError, "UTF-8"):
                wiki.page("Invalid.md")
            with self.assertRaisesRegex(WikiError, "page byte"):
                wiki.search("anything")

    def test_diff_raises_instead_of_truncating(self) -> None:
        newer = self.seed_entries({"Home.md": ("100644", b"a\n" * 2000)})
        with self.local_wiki("example/repository") as wiki:
            wiki.MAX_OUTPUT_BYTES = 1024
            with self.assertRaisesRegex(WikiError, "output budget"):
                wiki.diff(self.base, newer)

    def test_publish_race_is_rejected_by_exact_base_lease(self) -> None:
        with self.local_wiki("example/repository") as first, self.local_wiki("example/repository") as second:
            run = first._run
            concurrent = {}
            def race(*arguments: str, **keywords: object) -> bytes:
                if arguments[0] == "push":
                    concurrent.update(self.write(second, {"Other.md": "Concurrent writer"}))
                return run(*arguments, **keywords)
            with patch.object(first, "_run", side_effect=race), self.assertRaisesRegex(WikiError, "conflict or outcome unknown"):
                self.write(first, {"New.md": "Losing writer"})
            self.assertEqual(self.git(self.remote, "rev-parse", "HEAD").decode().strip(), concurrent["sha"])
            self.assertNotIn(b"New.md", self.git(self.remote, "ls-tree", "--name-only", "HEAD"))

    def test_post_push_verification_does_not_trust_local_commit(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            with patch.object(wiki, "_remote_tip", return_value=self.base), self.assertRaisesRegex(WikiError, "outcome unknown; re-read"):
                self.write(wiki, {"New.md": "Published but unconfirmed"})
            self.assertNotEqual(self.git(self.remote, "rev-parse", "HEAD").decode().strip(), self.base)
            self.assertEqual(wiki.snapshot()["sha"], self.base)

    def test_noop_rejects_changed_mode(self) -> None:
        newer = self.seed_entries({"Home.md": ("100755", b"# Home\nWelcome\n")})
        with self.local_wiki("example/repository") as wiki:
            with self.assertRaisesRegex(WikiError, "mode changed"):
                self.write(wiki, {"Home.md": "# Home\nWelcome\n"})
            self.assertEqual(self.git(self.remote, "rev-parse", "HEAD").decode().strip(), newer)

    def test_noop_rejects_rewritten_history(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            tree = self.git(self.remote, "rev-parse", "HEAD^{tree}").decode().strip()
            unrelated = self.git(self.remote, "commit-tree", tree, input_bytes=b"Unrelated root\n").decode().strip()
            self.git(self.remote, "update-ref", "refs/heads/docs/wiki", unrelated, self.base)
            with self.assertRaisesRegex(WikiError, "conflict"):
                self.write(wiki, {"Home.md": "# Home\nWelcome\n"})

    def test_cleanup_readonly_and_failed_entry(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            parent = wiki._parent
            readonly = parent / "readonly"
            readonly.write_bytes(b"fixture")
            readonly.chmod(stat.S_IREAD)
        self.assertFalse(parent.exists())
        with self.assertRaisesRegex(WikiError, "not open"):
            wiki.snapshot()
        failed = self.local_wiki("example/repository")
        failed.MAX_DISK_BYTES = 1
        with self.assertRaisesRegex(WikiError, "storage"):
            failed.__enter__()
        self.assertIsNone(failed._parent)

    def test_history_limit_and_empty_repository(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            for limit in (0, 101, -1, True, "5"):
                with self.assertRaises(WikiError):
                    wiki.history(limit=limit)
        self.git(self.remote, "update-ref", "-d", "refs/heads/docs/wiki", self.base)
        with self.local_wiki("example/repository") as wiki:
            self.assertEqual(wiki.snapshot(), {"repository": "example/repository", "branch": "docs/wiki", "sha": None, "initialized": False})
            with self.assertRaises(WikiError):
                wiki.pages()
            with self.assertRaisesRegex(WikiError, "initialized"):
                self.write(wiki, {"Home.md": "New"})

    def test_secret_is_env_only_and_package_import_is_independent(self) -> None:
        import issuelens_github_mcp.github as client
        self.assertIs(client.WikiRepository, WikiRepository)
        token = "test-token-not-a-credential"
        with self.local_wiki("example/repository", token=token) as wiki:
            recorded = []
            original = subprocess.Popen
            def record(command: list[str], **keywords: object) -> subprocess.Popen:
                recorded.append((command, dict(keywords["env"])))
                return original(command, **keywords)
            with patch("issuelens_github_mcp.wiki.subprocess.Popen", side_effect=record):
                wiki.page("Home.md")
            for command, environment in recorded:
                self.assertNotIn(token, repr(command))
                self.assertTrue(any("Authorization: Basic " in value for value in environment.values()))
            for path in wiki._parent.rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode(), path.read_bytes())


    def subprocess_fixture(self, script: str):
        original = subprocess.Popen
        def launch(command: list[str], **keywords: object) -> subprocess.Popen:
            return original([sys.executable, "-B", "-c", script], **keywords)
        return patch("issuelens_github_mcp.wiki.subprocess.Popen", side_effect=launch)

    def test_runner_bounds_both_streams_without_returning_secrets(self) -> None:
        scripts = ["import os; os.write(1, b'x' * 1000000)",
                   "import os; os.write(2, b'x' * 1000000)",
                   "import os; os.write(2, b'sensitive-fixture'); raise SystemExit(1)"]
        with self.local_wiki("example/repository") as wiki:
            for script in scripts:
                with self.subprocess_fixture(script), self.assertRaises(WikiError) as raised:
                    wiki._run("version", max_output=1024)
                self.assertNotIn("sensitive-fixture", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    def test_runner_deadline_kills_descendants_and_joins_readers(self) -> None:
        before = set(threading.enumerate())
        with self.local_wiki("example/repository") as wiki:
            script = "import subprocess, sys, threading; subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait(120)']); threading.Event().wait(120)"
            wiki._deadline = time.monotonic() + 0.5
            started = time.monotonic()
            with self.subprocess_fixture(script), self.assertRaisesRegex(WikiError, "time budget"):
                wiki._run("version")
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(set(threading.enumerate()), before)

    @unittest.skipUnless(os.name == "nt", "Windows suspended startup")
    def test_runner_cannot_spawn_before_job_assignment(self) -> None:
        before = set(threading.enumerate())
        with self.local_wiki("example/repository") as wiki:
            marker = wiki._parent / "child-started"
            ran_before_assignment = []

            class DelayedProcessTree(_ProcessTree):
                def __init__(self, process: subprocess.Popen[bytes]) -> None:
                    threading.Event().wait(0.5)
                    ran_before_assignment.append(marker.exists())
                    super().__init__(process)

            script = ("import subprocess, sys, threading; from pathlib import Path; "
                      "child = subprocess.Popen([sys.executable, '-B', '-c', "
                      "'import threading; threading.Event().wait(10)']); "
                      "Path('child-started').write_text(str(child.pid)); threading.Event().wait(10)")
            wiki._deadline = time.monotonic() + 2
            started = time.monotonic()
            with self.subprocess_fixture(script), patch("issuelens_github_mcp.wiki._ProcessTree", DelayedProcessTree):
                with self.assertRaisesRegex(WikiError, "time budget"):
                    wiki._run("version")
            self.assertEqual(ran_before_assignment, [False])
            self.assertTrue(marker.exists())
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(set(threading.enumerate()), before)

    def test_storage_watchdog_stops_running_process_not_just_finished_clone(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            current_size = sum(path.stat().st_size for path in wiki._parent.rglob("*") if path.is_file())
            wiki.MAX_DISK_BYTES = current_size + 1024
            script = "from pathlib import Path; import threading; Path('oversized-pack').write_bytes(b'x' * 131072); threading.Event().wait(120)"
            started = time.monotonic()
            with self.subprocess_fixture(script), self.assertRaisesRegex(WikiError, "storage budget"):
                wiki._run("version")
            self.assertLess(time.monotonic() - started, 5)

    def test_context_wide_output_and_command_budgets(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            wiki.MAX_TOTAL_OUTPUT_BYTES = wiki._output_bytes + 5
            with self.assertRaisesRegex(WikiError, "output budget"):
                wiki._run("rev-parse", "HEAD")
            wiki.MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
            wiki.MAX_COMMANDS = wiki._commands
            with self.assertRaisesRegex(WikiError, "command budget"):
                wiki._run("version")

    def test_cleanup_failure_is_reported_safely(self) -> None:
        wiki = self.local_wiki("example/repository").__enter__()
        parent = wiki._parent
        try:
            with patch("issuelens_github_mcp.wiki.shutil.rmtree", side_effect=PermissionError("private path")):
                with self.assertRaisesRegex(WikiError, "temporary storage cleanup failed") as raised:
                    wiki.__exit__(None, None, None)
            self.assertNotIn("private path", str(raised.exception))
            self.assertFalse(wiki._env)
        finally:
            wiki.__exit__(None, None, None)
        self.assertFalse(parent.exists())

    def test_existing_case_collision_is_rejected_on_read(self) -> None:
        self.seed_entries({"home.md": ("100644", b"Case collision")})
        with self.local_wiki("example/repository") as wiki:
            with self.assertRaisesRegex(WikiError, "collision"):
                wiki.pages()
            with self.assertRaisesRegex(WikiError, "collision"):
                wiki.page("Home.md")

    def test_twenty_pages_and_bounded_historical_results(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            result = self.write(wiki, {f"Page-{index}.md": "Page" for index in range(20)})
            self.assertEqual(len(result["pages"]), 20)
            self.assertEqual(wiki.history(limit=1, ref=self.base), [self.base])
            self.assertEqual(len(wiki.search("Page")), 20)
            wiki.MAX_TREE_ENTRIES = len(wiki.pages()) + 1
            with self.assertRaisesRegex(WikiError, "tree entry"):
                self.write(wiki, {"Overflow.md": "New"}, result["sha"])

    def test_constructor_and_default_remote_are_bounded(self) -> None:
        self.assertEqual(WikiRepository("example/repository").remote, "https://github.com/example/repository.wiki.git")
        for invalid in (None, "https://github.com/example/repository", "example/repository?url=x", "example/..", "../repository", "-owner/repo"):
            with self.assertRaises(WikiError):
                WikiRepository(invalid)
        for timeout in (0, -1, True, float("inf"), 301):
            with self.assertRaises(WikiError):
                WikiRepository("example/repository", timeout=timeout)
        for token in ("", "abc\ndef", "abc\x00", "\u00e9"):
            with self.assertRaises(WikiError):
                WikiRepository("example/repository", token=token)


@unittest.skipUnless(os.name == "nt", "Windows process isolation")
class WindowsProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        import ctypes

        self.ctypes = ctypes
        temporary = tempfile.TemporaryDirectory(prefix="wiki-process-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.wiki = WikiRepository("example/repository")
        self.wiki._parent = self.directory
        self.wiki._root = self.directory / "unused.git"
        self.wiki._env = self.wiki._environment(self.directory)
        self.wiki._deadline = time.monotonic() + 10
        self.original_popen = subprocess.Popen
        self.processes = []
        self.addCleanup(self.cleanup_processes)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        self.kernel.GetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.kernel.GetHandleInformation.restype = ctypes.c_int

    def launch(self, command: list[str], **keywords: object) -> subprocess.Popen:
        script = ("from pathlib import Path; import threading; "
                  "Path('started').write_bytes(b'started'); threading.Event().wait(10)")
        process = self.original_popen([sys.executable, "-B", "-c", script], **keywords)
        self.processes.append(process)
        return process

    def cleanup_processes(self) -> None:
        for process in self.processes:
            try:
                process.kill()
                process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def assert_startup_stopped(self, closed: Mock) -> None:
        self.assertFalse((self.directory / "started").exists())
        self.assertIsNotNone(self.processes[-1].poll())
        for stream in (self.processes[-1].stdin, self.processes[-1].stdout, self.processes[-1].stderr):
            self.assertTrue(stream.closed)
        for call in closed.call_args_list:
            flags = self.ctypes.c_uint32()
            self.assertFalse(self.kernel.GetHandleInformation(call.args[0], self.ctypes.byref(flags)))

    def test_startup_api_failures_kill_suspended_child_and_close_handles(self) -> None:
        failures = [("CreateJobObjectW", 0, 0), ("SetInformationJobObject", 0, 1),
                    ("AssignProcessToJobObject", 0, 1),
                    ("CreateToolhelp32Snapshot", self.ctypes.c_void_p(-1).value, 1),
                    ("Thread32First", 0, 2), ("Thread32Next", 0, 2),
                    ("OpenThread", 0, 2), ("ResumeThread", 0xFFFFFFFF, 3),
                    ("ResumeThread", 0, 3), ("ResumeThread", 2, 3)]
        before = set(threading.enumerate())
        for method, result, close_count in failures:
            with self.subTest(method=method, result=result):
                with patch("ctypes.WinDLL", return_value=self.kernel), \
                        patch.object(self.kernel, method, return_value=result), \
                        patch.object(self.kernel, "CloseHandle", wraps=self.kernel.CloseHandle) as closed, \
                        patch("issuelens_github_mcp.wiki.subprocess.Popen", side_effect=self.launch):
                    with self.assertRaises(WikiError):
                        self.wiki._run("version")
                self.assertEqual(closed.call_count, close_count)
                self.assert_startup_stopped(closed)
        self.assertEqual(set(threading.enumerate()), before)

    def test_startup_exceptions_close_handles_and_kill_suspended_child(self) -> None:
        for method, exception, close_count in (("CreateJobObjectW", OSError("fixture"), 0),
                                               ("AssignProcessToJobObject", OSError("fixture"), 1),
                                               ("OpenThread", OSError("fixture"), 2),
                                               ("ResumeThread", KeyboardInterrupt(), 3)):
            with self.subTest(method=method):
                with patch("ctypes.WinDLL", return_value=self.kernel), \
                        patch.object(self.kernel, method, side_effect=exception), \
                        patch.object(self.kernel, "CloseHandle", wraps=self.kernel.CloseHandle) as closed, \
                        patch("issuelens_github_mcp.wiki.subprocess.Popen", side_effect=self.launch):
                    with self.assertRaises(type(exception)):
                        self.wiki._run("version")
                self.assertEqual(closed.call_count, close_count)
                self.assert_startup_stopped(closed)

    def test_process_tree_close_is_idempotent(self) -> None:
        process = self.launch([], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=self.directory, env=self.wiki._env, creationflags=0x4)
        with patch("ctypes.WinDLL", return_value=self.kernel), \
                patch.object(self.kernel, "CloseHandle", wraps=self.kernel.CloseHandle) as closed:
            tree = _ProcessTree(process)
            self.addCleanup(tree.close)
            self.assertEqual(closed.call_count, 2)
            tree.close()
            tree.close()
            self.assertEqual(closed.call_count, 3)
        process.wait(timeout=5)
        self.assertIsNone(tree._handle)


if __name__ == "__main__":
    unittest.main()