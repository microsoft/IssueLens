from __future__ import annotations

import os
import stat
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dulwich.client import LocalGitClient
from dulwich.config import StackedConfig
from dulwich.objects import Blob, Commit, Tree
from issuelens_github_mcp.wiki import WikiError, WikiRepository
from test_wiki_transport import WikiFixture


class WikiTests(WikiFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.unicode_path = "notes/\u8bbe\u8ba1-\u00e9.md"
        self.base = self.seed_entries({"images/logo.bin": ("100644", bytes(range(256))),
                                       self.unicode_path: ("100644", "# \u8bbe\u8ba1\nCaf\u00e9\n".encode("utf-8"))}, parents=[])

    def test_bare_snapshot_and_unicode_reads(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            self.assertEqual(wiki.snapshot(), {"repository": "example/repository", "branch": "docs/wiki",
                                               "sha": self.base, "initialized": True})
            self.assertEqual([page["path"] for page in wiki.pages()], ["Home.md", self.unicode_path])
            page = wiki.page(self.unicode_path)
            self.assertEqual(page["content"], "# \u8bbe\u8ba1\nCaf\u00e9\n")
            self.assertEqual(page["ref"], self.base)
            self.assertEqual(page["sha"], self.object_id(self.base, self.unicode_path))
            self.assertFalse((wiki._parent / "Home.md").exists())
            self.assertTrue(wiki._repo.bare)
            parent = wiki._parent
        self.assertFalse(parent.exists())

    def test_unicode_default_branch_supports_reads_and_atomic_writes(self) -> None:
        for branch in (
            "\u6587\u6863/wiki", "release/Cafe\u0301", "notes/\u00e9\u00a0",
            "notes/\u2028wiki", "notes/\u2029wiki",
        ):
            with self.subTest(branch=branch):
                branch_ref = f"refs/heads/{branch}"
                self.remote_repo.refs[branch_ref.encode("utf-8")] = self.base.encode("ascii")
                self.remote_repo.refs.set_symbolic_ref(b"HEAD", branch_ref.encode("utf-8"))
                with self.local_wiki("example/repository") as wiki:
                    self.assertEqual(wiki.snapshot()["branch"], branch)
                    self.assertEqual(wiki.snapshot()["sha"], self.base)
                    self.assertIn("Home.md", [page["path"] for page in wiki.pages()])
                    self.assertEqual(wiki.page("Home.md")["content"], "# Home\nWelcome\n")
                    self.assertIn(self.unicode_path, [page["path"] for page in wiki.search("Caf\u00e9")])
                    self.assertEqual(wiki.history(ref=self.base), [self.base])
                    self.assertEqual(wiki.diff(self.base), "")
                    updated = self.write(wiki, {"Home.md": "Unicode branch update\n"})
                    self.assertEqual(updated["status"], "updated")
                    self.assertEqual(updated["branch"], branch)
                    self.assertEqual(self.tip(branch_ref.encode("utf-8")), updated["sha"])
                    self.assertIn("+Unicode branch update", wiki.diff(self.base))
                    self.assertEqual(wiki.history("Home.md"), [updated["sha"], self.base])
                    self.assertEqual(wiki.page("Home.md", self.base)["content"], "# Home\nWelcome\n")
                    retry = self.write(wiki, {"Home.md": "Unicode branch update\n"})
                    self.assertEqual(retry["status"], "no-change")
                    self.assertEqual(retry["sha"], updated["sha"])
                    with self.assertRaisesRegex(WikiError, "conflict"):
                        self.write(wiki, {"Home.md": "Conflicting update"})
                self.assertEqual(self.tip(), self.base)

    def test_default_branch_limit_counts_utf8_bytes_and_rejects_controls(self) -> None:
        branch = "\u00e9" * 100
        branch_ref = f"refs/heads/{branch}"
        self.remote_repo.refs[branch_ref.encode("utf-8")] = self.base.encode("ascii")
        self.remote_repo.refs.set_symbolic_ref(b"HEAD", branch_ref.encode("utf-8"))
        with self.local_wiki("example/repository") as wiki:
            self.assertEqual(wiki.snapshot()["branch"], branch)
        for invalid in ("\u00e9" * 100 + "a", "\u6587" * 67, "notes/\u200b", "notes/\u0085"):
            with self.subTest(branch=ascii(invalid)):
                wiki = self.local_wiki("example/repository")
                original_fetch = LocalGitClient.fetch_pack

                def advertised_branch(client, *arguments, **kwargs):
                    result = original_fetch(client, *arguments, **kwargs)
                    result.symrefs[b"HEAD"] = f"refs/heads/{invalid}".encode("utf-8")
                    return result

                with patch.object(LocalGitClient, "fetch_pack", new=advertised_branch):
                    with self.assertRaisesRegex(WikiError, "invalid wiki default branch"):
                        with wiki:
                            self.fail("Invalid default branch was accepted")
                self.assertIsNone(wiki._root)
                self.assertIsNone(wiki._parent)

    def test_atomic_write_preserves_content_and_supports_reads(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            old_home = wiki.page("Home.md")
            old_asset = self.object_id(self.base, "images/logo.bin")
            updated = self.write(wiki, {self.unicode_path: "# Revised\nCaf\u00e9\n", "New.md": "New page\n"})
            self.assertEqual(set(updated), {"status", "sha", "branch", "pages", "repository"})
            self.assertEqual(updated["status"], "updated")
            self.assertEqual(updated["branch"], "docs/wiki")
            self.assertEqual(wiki.page("Home.md")["sha"], old_home["sha"])
            self.assertEqual(self.object_id(updated["sha"], "images/logo.bin"), old_asset)
            self.assertEqual(self.remote_repo.object_store[updated["sha"].encode("ascii")].parents, [self.base.encode("ascii")])
            self.assertEqual(wiki.history(), [updated["sha"], self.base])
            self.assertEqual(wiki.page(self.unicode_path, self.base)["content"], "# \u8bbe\u8ba1\nCaf\u00e9\n")
            self.assertEqual(wiki.history(ref=self.base), [self.base])
            self.assertEqual(wiki.history(self.unicode_path), [updated["sha"], self.base])
            self.assertEqual(wiki.history(limit=1), [updated["sha"]])
            self.assertEqual([page["path"] for page in wiki.search("CAF\u00c9")], [self.unicode_path])
            self.assertIn("+# Revised", wiki.diff(self.base))
            self.assertEqual(wiki.diff(updated["sha"]), "")
            self.assertEqual(self.remote_repo.object_store[updated["sha"].encode("ascii")].author,
                             b"IssueLens App <123+issuelens[bot]@users.noreply.github.com>")

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
            self.assertEqual(self.tip(), updated["sha"])

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
            self.assertEqual(self.tip(), self.base)
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
            self.assertEqual(self.tip(), base)
            result = self.write(wiki, {"Valid.md": "safe"}, base)
            self.assertEqual(self.object_id(result["sha"], "link.md"), self.object_id(base, "link.md"))

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
        hostile = {"GIT_DIR": str(self.directory / "missing.git"), "GIT_WORK_TREE": str(self.directory),
                   "GIT_INDEX_FILE": str(self.directory / "outside-index"), "GIT_CONFIG_GLOBAL": str(hostile_config),
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "include.path", "GIT_CONFIG_VALUE_0": str(hostile_config),
                   "GIT_EXEC_PATH": str(self.directory), "GIT_SSH_COMMAND": "invalid-command", "SSH_ASKPASS": "invalid-command",
                   "GIT_ASKPASS": "invalid-command", "GCM_CREDENTIAL_STORE": "plaintext", "GIT_TRACE": str(self.directory / "trace")}
        with patch.dict(os.environ, hostile), patch.object(StackedConfig, "default", side_effect=AssertionError("ambient config")), self.local_wiki("example/repository") as wiki:
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
            send_pack = first._client.send_pack
            concurrent = {}

            def race(*arguments, **keywords):
                concurrent.update(self.write(second, {"Other.md": "Concurrent writer"}))
                return send_pack(*arguments, **keywords)
            with patch.object(first._client, "send_pack", side_effect=race), self.assertRaisesRegex(WikiError, "conflict or outcome unknown"):
                self.write(first, {"New.md": "Losing writer"})
            self.assertEqual(self.tip(), concurrent["sha"])
            with self.assertRaises(KeyError):
                self.object_id(self.tip(), "New.md")

    def test_post_push_verification_does_not_trust_local_commit(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            with patch.object(wiki, "_remote_tip", return_value=self.base), self.assertRaisesRegex(WikiError, "outcome unknown; re-read"):
                self.write(wiki, {"New.md": "Published but unconfirmed"})
            self.assertNotEqual(self.tip(), self.base)
            self.assertEqual(wiki.snapshot()["sha"], self.base)

    def test_post_push_default_branch_switch_is_not_confirmed(self) -> None:
        alternate = b"refs/heads/new-default"
        for same_commit in (False, True):
            with self.subTest(same_commit=same_commit):
                base = self.tip()
                self.remote_repo.refs[alternate] = base.encode("ascii")
                with self.local_wiki("example/repository") as wiki:
                    send_pack = wiki._transport.send_pack

                    def switch_default(*arguments, **keywords):
                        result = send_pack(*arguments, **keywords)
                        if same_commit:
                            self.remote_repo.refs[alternate] = self.remote_repo.refs[self.branch]
                        self.remote_repo.refs.set_symbolic_ref(b"HEAD", alternate)
                        return result

                    try:
                        with patch.object(wiki._transport, "send_pack", side_effect=switch_default):
                            with self.assertRaisesRegex(WikiError, "outcome unknown; re-read"):
                                self.write(wiki, {"New.md": f"Unconfirmed {same_commit}"}, base)
                        self.assertNotEqual(self.tip(), base)
                        self.assertEqual(wiki.snapshot()["sha"], base)
                        self.assertEqual(self.remote_repo.refs.read_ref(b"HEAD"), b"ref: " + alternate)
                        self.assertEqual(self.tip(alternate), self.tip() if same_commit else base)
                    finally:
                        self.remote_repo.refs.set_symbolic_ref(b"HEAD", self.branch)

    def test_remote_tip_rejects_missing_or_inconsistent_head_advertisement(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            original = wiki._transport.get_refs(wiki._transport_path, protocol_version=2)
            for invalid in (
                "missing_symrefs", "missing_head_symref", "different_head_symref",
                "missing_head_sha", "different_head_sha", "missing_branch", "invalid_branch_sha",
            ):
                with self.subTest(advertisement=invalid):
                    advertised = SimpleNamespace(refs=dict(original.refs), symrefs=dict(original.symrefs))
                    if invalid == "missing_symrefs":
                        advertised.symrefs = None
                    elif invalid == "missing_head_symref":
                        del advertised.symrefs[b"HEAD"]
                    elif invalid == "different_head_symref":
                        advertised.symrefs[b"HEAD"] = b"refs/heads/another"
                    elif invalid == "missing_head_sha":
                        del advertised.refs[b"HEAD"]
                    elif invalid == "different_head_sha":
                        advertised.refs[b"HEAD"] = b"0" * 40
                    elif invalid == "missing_branch":
                        del advertised.refs[self.branch]
                    else:
                        advertised.refs[self.branch] = advertised.refs[b"HEAD"] = b"invalid"
                    with patch.object(wiki._transport, "get_refs", return_value=advertised):
                        with self.assertRaisesRegex(WikiError, "could not be verified"):
                            wiki._remote_tip()

    def test_noop_rejects_changed_mode(self) -> None:
        newer = self.seed_entries({"Home.md": ("100755", b"# Home\nWelcome\n")})
        with self.local_wiki("example/repository") as wiki:
            with self.assertRaisesRegex(WikiError, "mode changed"):
                self.write(wiki, {"Home.md": "# Home\nWelcome\n"})
            self.assertEqual(self.tip(), newer)

    def test_noop_rejects_rewritten_history(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            self.seed_entries({}, parents=[])
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

    def _merge_history(self, timestamps=(0, 10, 20, 30)):
        commits = []
        for timestamp, parents in zip(timestamps, ([], [0], [0], [1, 2])):
            blob = Blob.from_string(f"Page at {timestamp}\n".encode("ascii"))
            second_page = Blob.from_string(b"Original\n" if len(commits) < 2 else b"Second parent change\n")
            tree = Tree()
            tree.add(b"Home.md", 0o100644, blob.id)
            tree.add(b"Second.md", 0o100644, second_page.id)
            commit = Commit()
            commit.tree = tree.id
            commit.parents = [commits[index].id for index in parents]
            commit.author = commit.committer = b"Fixture <fixture@example.test>"
            commit.author_time = commit.commit_time = timestamp
            commit.author_timezone = commit.commit_timezone = 0
            commit.message = f"Commit at {timestamp}\n".encode("ascii")
            for obj in (blob, second_page, tree, commit):
                self.remote_repo.object_store.add_object(obj)
            commits.append(commit)
        self.remote_repo.refs[self.branch] = commits[3].id
        return commits

    def test_history_orders_merge_parents_before_applying_limit(self) -> None:
        commits = self._merge_history()
        with self.local_wiki("example/repository") as wiki:
            expected = [commits[index].id.decode("ascii") for index in (3, 2, 1)]
            self.assertEqual(wiki.history(limit=3), expected)
            self.assertEqual(wiki.history("Home.md", limit=3), expected)
            self.assertEqual(wiki.history("Second.md", limit=1), [commits[2].id.decode("ascii")])
            self.assertEqual(wiki.history("Second.md"),
                             [commits[index].id.decode("ascii") for index in (2, 0)])
            self.assertEqual(wiki.history(ref=commits[1].id.decode("ascii")),
                             [commits[index].id.decode("ascii") for index in (1, 0)])

    def test_history_keeps_children_before_clock_skewed_parents(self) -> None:
        commits = self._merge_history((50, 40, 20, 30))
        with self.local_wiki("example/repository") as wiki:
            expected = [commits[index].id.decode("ascii") for index in (3, 1, 2, 0)]
            self.assertEqual(wiki.history(), expected)
            self.assertEqual(wiki.history(limit=3), expected[:3])
            self.assertEqual(wiki.history("Second.md", limit=1), [commits[2].id.decode("ascii")])

    def test_history_bounds_full_traversal_before_small_limit(self) -> None:
        self._merge_history()
        with self.local_wiki("example/repository") as wiki:
            wiki.MAX_DISK_ENTRIES = 3
            for path in (None, "Second.md"):
                with self.subTest(path=path), self.assertRaisesRegex(WikiError, "history traversal budget"):
                    wiki.history(path, limit=1)

    def test_history_checks_deadline_during_ordering(self) -> None:
        self._merge_history()
        with self.local_wiki("example/repository") as wiki:
            check_budget = wiki._check_budget
            checks = 0

            def expire_during_walk():
                nonlocal checks
                checks += 1
                if checks == 4:
                    wiki._deadline = time.monotonic() - 1
                check_budget()

            with patch.object(wiki, "_check_budget", side_effect=expire_during_walk):
                with self.assertRaisesRegex(WikiError, "time budget"):
                    wiki.history(limit=1)
            self.assertEqual(checks, 4)

    def test_history_limit_and_empty_repository(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            for limit in (0, 101, -1, True, "5"):
                with self.assertRaises(WikiError):
                    wiki.history(limit=limit)
        del self.remote_repo.refs[self.branch]
        with self.local_wiki("example/repository") as wiki:
            self.assertEqual(wiki.snapshot(), {"repository": "example/repository", "branch": "docs/wiki", "sha": None, "initialized": False})
            with self.assertRaises(WikiError):
                wiki.pages()
            with self.assertRaisesRegex(WikiError, "initialized"):
                self.write(wiki, {"Home.md": "New"})

    def test_secret_is_not_in_config_storage_or_results(self) -> None:
        import issuelens_github_mcp.github as client
        self.assertIs(client.WikiRepository, WikiRepository)
        token = "test-token-not-a-credential"
        with self.local_wiki("example/repository", token=token) as wiki:
            self.assertNotIn(token, repr(wiki))
            self.assertNotIn(token, repr(wiki._repo.get_config()))
            self.assertNotIn(token, repr(wiki.page("Home.md")))
            self.assertNotIn(token, wiki.remote)
            for path in wiki._parent.rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode(), path.read_bytes())

    def test_context_wide_output_request_and_time_budgets(self) -> None:
        with self.local_wiki("example/repository") as wiki:
            wiki.MAX_TOTAL_OUTPUT_BYTES = wiki._output_bytes + 5
            with self.assertRaisesRegex(WikiError, "output budget"):
                wiki._progress(b"123456")
            wiki.MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
            wiki.MAX_REQUESTS = wiki._requests
            with self.assertRaisesRegex(WikiError, "request budget"):
                wiki._refresh()
            wiki._deadline = time.monotonic() - 1
            with self.assertRaisesRegex(WikiError, "time budget"):
                wiki.page("Home.md")

    def test_cleanup_failure_is_reported_safely(self) -> None:
        wiki = self.local_wiki("example/repository").__enter__()
        parent = wiki._parent
        try:
            with patch("issuelens_github_mcp.wiki.shutil.rmtree", side_effect=PermissionError("private path")):
                with self.assertRaises(WikiError) as raised:
                    wiki.__exit__(None, None, None)
            self.assertNotIn("private path", str(raised.exception))
            self.assertIsNone(wiki._repo)
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


if __name__ == "__main__":
    unittest.main()
