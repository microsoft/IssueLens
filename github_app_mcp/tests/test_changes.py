import asyncio
import base64
import hashlib
import json
import logging
import os
import pathlib
import random
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from mcp import Client


PACKAGE_ROOT = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, os.fspath(PACKAGE_ROOT))

from issuelens_github_mcp import changes  # noqa: E402
from issuelens_github_mcp.auth import GitHubAppError, InstallationCredential  # noqa: E402
from issuelens_github_mcp.github import GitHubClient  # noqa: E402
from issuelens_github_mcp.server import create_server  # noqa: E402


REPOSITORY = "owner/repo"


def oid(value):
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()


class Provider:
    def __init__(self, available=True):
        self.calls = []
        self.available = available
        self.repository_override = None
        self.permissions_override = None
        self.expires_at = float("inf")

    async def get_token(self, repository, permissions):
        self.calls.append((repository, dict(permissions)))
        if not self.available:
            raise GitHubAppError("installation unavailable")
        return InstallationCredential(
            installation_id=1,
            repository=self.repository_override or repository,
            permissions=self.permissions_override or tuple(sorted(permissions.items())),
            token="test-app-credential",
            expires_at=self.expires_at,
        )


class Objects:
    def __init__(self):
        self.payloads = {}
        self.requests = []
        self.public = True
        self.overrides = {}
        self.provider = Provider()

    def file(self, content, *, mode="100644"):
        data = content.encode("utf-8") if isinstance(content, str) else content
        sha = hashlib.sha1(
            f"blob {len(data)}\0".encode("ascii") + data, usedforsecurity=False,
        ).hexdigest()
        self.payloads[f"/git/blobs/{sha}"] = {
            "sha": sha, "size": len(data), "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }
        return {"sha": sha, "type": "blob", "mode": mode, "size": len(data)}

    def tree(self, files):
        entries = [{"path": name, **entry} for name, entry in files.items()]
        sha = oid("tree:" + json.dumps(entries, sort_keys=True))
        self.payloads[f"/git/trees/{sha}"] = {"sha": sha, "tree": entries, "truncated": False}
        return sha

    def directory(self, sha):
        return {"sha": sha, "type": "tree", "mode": "040000"}

    def commit(self, tree, parents=(), label=""):
        sha = oid(f"commit:{tree}:{','.join(parents)}:{label}")
        self.payloads[f"/git/commits/{sha}"] = {
            "sha": sha, "tree": {"sha": tree},
            "parents": [{"sha": parent} for parent in parents], "message": label,
        }
        return sha

    def pull(self, base, head, number=7):
        self.payloads[f"/pulls/{number}"] = {
            "number": number, "base": {"sha": base},
            "head": {"sha": head, "repo": {"full_name": "outside/fork"}},
        }

    def handler(self, request):
        self.requests.append(request)
        if request.url.host != "api.github.com" or request.method != "GET":
            raise AssertionError(f"Unexpected method or host: {request.method} {request.url}")
        prefix = f"/repos/{REPOSITORY}"
        if not request.url.path.casefold().startswith(prefix):
            raise AssertionError(f"Unexpected repository: {request.url}")
        route = request.url.path[len(prefix):]
        if request.url.query and not (
            (route.endswith("/files") and dict(request.url.params) == {"per_page": "30", "page": "1"})
            or (route.startswith("/git/trees/") and dict(request.url.params) == {"recursive": "1"})
            or (route == "/commits" and set(request.url.params) == {"sha", "per_page"} and request.url.params["per_page"] == "1")
        ):
            raise AssertionError(f"Unexpected query: {request.url}")
        if route in self.overrides:
            response = self.overrides[route]
            return response(request) if callable(response) else response
        if route == "":
            return httpx.Response(200, json={"full_name": REPOSITORY, "private": not self.public})
        if route in self.payloads:
            return httpx.Response(200, json=self.payloads[route])
        if route.startswith(("/commits/", "/compare/")) or route.endswith("/files"):
            return httpx.Response(200, json={"files": [{"patch": "x" * 400_000}]})
        return httpx.Response(404, json={"message": "not found"})

    def client(self):
        return GitHubClient(self.provider, transport=httpx.MockTransport(self.handler))

    def routes(self):
        return [request.url.path[len(f"/repos/{REPOSITORY}"):] for request in self.requests]


class ChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.objects = Objects()
        self.client = self.objects.client()
        logger = logging.getLogger("httpx")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)

    async def diff_pages(self, base, head, path, *, max_bytes=4096, client=None):
        client = client or self.client
        cursor = None
        pages, seen = [], set()
        while True:
            page = await client.read_diff_chunk(
                REPOSITORY, base, head, path, cursor=cursor, max_bytes=max_bytes,
            )
            self.assertLessEqual(changes.serialized_size(page), max_bytes)
            self.assertEqual(page["complete"], page["next_cursor"] is None)
            self.assertNotIn(page["chunk_id"], seen)
            seen.add(page["chunk_id"])
            pages.append(page)
            if page["complete"]:
                return pages
            self.assertTrue(page["content"])
            self.assertNotEqual(cursor, page["next_cursor"])
            cursor = page["next_cursor"]

    async def test_first_parent_inventory_uses_only_lightweight_objects(self):
        old = self.objects.file("old\n")
        new = self.objects.file("new\n")
        empty = self.objects.tree({})
        parent = self.objects.commit(self.objects.tree({"code.py": old, "removed.py": old}))
        other_parent = self.objects.commit(empty, label="other-parent")
        head = self.objects.commit(
            self.objects.tree({"code.py": new, "added.py": new}), [parent, other_parent],
        )
        result = await self.client.list_change_files(REPOSITORY, commit_sha=head)
        self.assertEqual(result["snapshot"], {
            "snapshot_id": changes.snapshot_id(REPOSITORY, parent, head),
            "base_sha": parent, "head_sha": head, "mode": "commit",
        })
        self.assertEqual([item["path"] for item in result["files"]], ["added.py", "code.py", "removed.py"])
        self.assertEqual([item["status"] for item in result["files"]], ["added", "modified", "removed"])
        self.assertEqual(result["files"][1], {
            "path": "code.py", "status": "modified",
            "old_blob_sha": old["sha"], "new_blob_sha": new["sha"],
            "old_mode": "100644", "new_mode": "100644",
        })
        self.assertTrue(result["complete"])
        self.assertIsNone(result["next_cursor"])
        self.assertTrue(all(route.startswith(("/git/commits/", "/git/trees/")) for route in self.objects.routes()))
        self.assertNotIn(f"/git/commits/{other_parent}", self.objects.routes())
        self.assertTrue(all(permissions == {"contents": "read"} for _, permissions in self.objects.provider.calls))

    async def test_root_commit_empty_files_and_mode_only_changes_are_explicit(self):
        empty_file = self.objects.file("")
        root = self.objects.commit(self.objects.tree({"empty.txt": empty_file}))
        inventory = await self.client.list_change_files(REPOSITORY, commit_sha=root)
        self.assertIsNone(inventory["snapshot"]["base_sha"])
        self.assertIsNone(inventory["files"][0]["old_blob_sha"])
        diff = await self.client.read_diff_chunk(REPOSITORY, None, root, "empty.txt")
        self.assertEqual(diff["status"], "text")
        self.assertIn("new mode 100644", diff["content"])
        self.assertEqual(diff["snapshot_id"], inventory["snapshot"]["snapshot_id"])
        source = await self.client.read_file_range(REPOSITORY, root, "empty.txt")
        self.assertEqual((source["content"], source["start_line"], source["end_line"], source["complete"]), ("", 0, 0, True))
        executable = {**empty_file, "mode": "100755"}
        next_commit = self.objects.commit(self.objects.tree({"empty.txt": executable}), [root])
        changed = await self.client.list_change_files(REPOSITORY, commit_sha=next_commit)
        self.assertEqual(changed["files"][0]["status"], "modified")
        mode = await self.client.read_diff_chunk(REPOSITORY, root, next_commit, "empty.txt")
        self.assertIn("old mode 100644\nnew mode 100755", mode["content"])
        unchanged = await self.client.read_diff_chunk(REPOSITORY, root, root, "empty.txt")
        self.assertEqual(unchanged["content"], "")
        self.assertTrue(unchanged["complete"])

    async def test_pr_uses_true_merge_base_and_cursor_never_repins_moved_pr(self):
        old = self.objects.file("old\n")
        new = self.objects.file("new\n")
        ancestor = self.objects.commit(self.objects.tree({"a": old, "b": old}))
        base_tip = self.objects.commit(self.objects.tree({"a": old, "b": old, "base-only": new}), [ancestor])
        head = self.objects.commit(self.objects.tree({"a": new, "b": new}), [ancestor])
        self.objects.pull(base_tip, head)
        first = await self.client.list_change_files(REPOSITORY, pull_number=7, per_page=1)
        self.assertEqual(first["snapshot"]["base_sha"], ancestor)
        self.assertNotEqual(first["snapshot"]["base_sha"], base_tip)
        self.assertEqual(first["snapshot"]["mode"], "pull_request")
        self.assertEqual(first["snapshot"]["pull_number"], 7)
        self.assertEqual(first["files"][0]["path"], "a")
        moved = self.objects.commit(self.objects.tree({"moved": new}), [head])
        self.objects.pull(base_tip, moved)
        requests_before = len(self.objects.requests)
        second = await self.objects.client().list_change_files(
            REPOSITORY, pull_number=7, cursor=first["next_cursor"], per_page=1,
        )
        self.assertEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(second["files"][0]["path"], "b")
        self.assertTrue(second["complete"])
        self.assertFalse(any(request.url.path.endswith("/pulls/7") for request in self.objects.requests[requests_before:]))
        explicit = await self.client.list_change_files(REPOSITORY, base_sha=ancestor, head_sha=head)
        diff = await self.client.read_diff_chunk(REPOSITORY, ancestor, head, "a")
        self.assertEqual(explicit["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"])
        self.assertEqual(diff["snapshot_id"], first["snapshot"]["snapshot_id"])
        self.assertIn((REPOSITORY, {"pull_requests": "read"}), self.objects.provider.calls)
        self.assertTrue(all(
            permissions in ({"contents": "read"}, {"pull_requests": "read"})
            for _, permissions in self.objects.provider.calls
        ))

    async def test_merge_base_stops_at_shared_history_not_repository_root(self):
        tree = self.objects.tree({})
        unvisited = oid("unavailable-old-history")
        ancestor = self.objects.commit(tree, [unvisited], "ancestor")
        base = self.objects.commit(tree, [ancestor], "base")
        head = self.objects.commit(tree, [ancestor], "head")
        self.objects.pull(base, head)
        result = await self.client.list_change_files(REPOSITORY, pull_number=7)
        self.assertEqual(result["snapshot"]["base_sha"], ancestor)
        self.assertNotIn(f"/git/commits/{unvisited}", self.objects.routes())

    async def test_merge_base_matches_complete_ground_truth_for_bounded_merge_graphs(self):
        randomizer = random.Random(17)
        for trial in range(30):
            objects = Objects()
            tree = objects.tree({})
            commits, ancestors = [], {}
            for index in range(20):
                parents = randomizer.sample(commits, min(len(commits), randomizer.randrange(1, 4)))
                sha = objects.commit(tree, parents, str(index))
                commits.append(sha)
                ancestors[sha] = {sha}.union(*(ancestors[parent] for parent in parents))
            reader = objects.client()._changes
            for _ in range(20):
                base, head = randomizer.sample(commits, 2)
                common = ancestors[base] & ancestors[head]
                expected = {
                    sha for sha in common
                    if not any(sha != other and sha in ancestors[other] for other in common)
                }
                with self.subTest(trial=trial, base=commits.index(base), head=commits.index(head)):
                    async with reader._session(REPOSITORY) as session:
                        if len(expected) != 1:
                            with self.assertRaises(GitHubAppError):
                                await reader._merge_base(session, base, head)
                        else:
                            self.assertEqual(await reader._merge_base(session, base, head), expected.pop())

    async def test_ambiguous_missing_and_over_budget_merge_bases_fail(self):
        tree = self.objects.tree({})
        root = self.objects.commit(tree)
        left = self.objects.commit(tree, [root], "left")
        right = self.objects.commit(tree, [root], "right")
        base = self.objects.commit(tree, [left, right], "base")
        head = self.objects.commit(tree, [right, left], "head")
        self.objects.pull(base, head)
        with self.assertRaisesRegex(GitHubAppError, "unambiguous"):
            await self.client.list_change_files(REPOSITORY, pull_number=7)
        separate = self.objects.commit(tree, label="separate root")
        self.objects.pull(root, separate)
        with self.assertRaisesRegex(GitHubAppError, "unambiguous"):
            await self.objects.client().list_change_files(REPOSITORY, pull_number=7)
        self.objects.pull(base, head)
        with patch.object(changes, "MAX_CHANGE_GRAPH_COMMITS", 2):
            with self.assertRaisesRegex(GitHubAppError, "graph budget"):
                await self.objects.client().list_change_files(REPOSITORY, pull_number=7)
        self.objects.pull(base, oid("inaccessible-fork-object"))
        with self.assertRaisesRegex(GitHubAppError, "HTTP 404"):
            await self.objects.client().list_change_files(REPOSITORY, pull_number=7)
        self.assertTrue(all(request.url.path.startswith("/repos/owner/repo/") for request in self.objects.requests))

    async def test_changed_trees_skip_identical_subtrees_and_handle_directory_transitions(self):
        old, new = self.objects.file("old\n"), self.objects.file("new\n")
        shared = self.objects.tree({"unread": old})
        nested = self.objects.tree({"child": new})
        before = self.objects.commit(self.objects.tree({
            "same": self.objects.directory(shared), "becomes-dir": old,
            "becomes-file": self.objects.directory(nested),
        }))
        after = self.objects.commit(self.objects.tree({
            "same": self.objects.directory(shared), "becomes-dir": self.objects.directory(nested),
            "becomes-file": new,
        }), [before])
        result = await self.client.list_change_files(REPOSITORY, commit_sha=after)
        self.assertEqual([(item["path"], item["status"]) for item in result["files"]], [
            ("becomes-dir", "removed"), ("becomes-dir/child", "added"),
            ("becomes-file", "added"), ("becomes-file/child", "removed"),
        ])
        for item in result["files"]:
            with self.subTest(path=item["path"]):
                pages = await self.diff_pages(before, after, item["path"], max_bytes=1024)
                for page in pages:
                    self.assertEqual(page["status"], "text")
                    self.assertEqual(page["snapshot_id"], result["snapshot"]["snapshot_id"])
                    self.assertEqual(page["old_blob_sha"], item["old_blob_sha"])
                    self.assertEqual(page["new_blob_sha"], item["new_blob_sha"])
                content = "".join(page["content"] for page in pages)
                self.assertIn("+new\n" if item["status"] == "added" else (
                    "-old\n" if item["path"] == "becomes-dir" else "-new\n"
                ), content)
        self.assertNotIn(f"/git/trees/{shared}", self.objects.routes())

    async def test_directory_only_diff_targets_remain_explicitly_unsupported(self):
        before = self.objects.commit(self.objects.tree({}))
        directory = self.objects.directory(self.objects.tree({
            "child": self.objects.file("new\n"),
        }))
        after = self.objects.commit(self.objects.tree({"dir": directory}), [before])
        for base in (before, after):
            with self.subTest(base=base):
                page = await self.client.read_diff_chunk(REPOSITORY, base, after, "dir")
                self.assertEqual(page["status"], "unsupported")
                self.assertEqual(page["reason"], "directory")

    async def test_missing_descendants_never_follow_files_links_or_gitlinks(self):
        entries = {
            "file": self.objects.file("not a directory"),
            "link": self.objects.file("outside/target", mode="120000"),
            "submodule": {"sha": oid("other-repository"), "mode": "160000", "type": "commit"},
        }
        head = self.objects.commit(self.objects.tree(entries))
        for path in entries:
            with self.subTest(path=path):
                with self.assertRaisesRegex(GitHubAppError, "non-directory ancestor"):
                    await self.client.read_file_range(REPOSITORY, head, f"{path}/child")
                with self.assertRaisesRegex(GitHubAppError, "does not exist"):
                    await self.client.read_diff_chunk(REPOSITORY, None, head, f"{path}/child")
        self.assertFalse(any(route.startswith("/git/blobs/") for route in self.objects.routes()))

    async def test_inventory_exceeds_3000_without_patch_endpoint_or_global_cap_change(self):
        item = self.objects.file("small\n")
        tree = self.objects.tree({f"file-{index:05}.txt": item for index in range(3501)})
        self.assertGreater(len(json.dumps(self.objects.payloads[f"/git/trees/{tree}"])), 128 * 1024)
        head = self.objects.commit(tree)
        all_files, cursor = [], None
        while True:
            page = await self.client.list_change_files(REPOSITORY, commit_sha=head, cursor=cursor, per_page=100)
            self.assertLessEqual(changes.serialized_size(page), changes.MAX_CHANGE_RESULT_BYTES)
            all_files.extend(page["files"])
            cursor = page["next_cursor"]
            if page["complete"]:
                break
        self.assertEqual(len(all_files), 3501)
        self.assertEqual(len({item["path"] for item in all_files}), 3501)
        self.assertEqual(len(self.objects.requests), 2)
        self.assertFalse(any("patch" in item for item in all_files))
        for operation in (
            lambda: self.client.get_commit(REPOSITORY, head),
            lambda: self.client.list_pull_request_files(REPOSITORY, 7),
            lambda: self.client.compare_commits(REPOSITORY, head, head),
        ):
            with self.assertRaisesRegex(GitHubAppError, "response is too large"):
                await operation()

    async def test_two_mib_aggregate_and_one_mib_single_diff_pages_are_lossless_and_cached(self):
        one_mib = "x" * (1024 * 1024 - 1) + "\n"
        source_a = one_mib
        source_b = "y" * (1024 * 1024 - 1) + "\n"
        tree = self.objects.tree({"a.txt": self.objects.file(source_a), "b.txt": self.objects.file(source_b)})
        head = self.objects.commit(tree)
        manifest = await self.client.list_change_files(REPOSITORY, commit_sha=head)
        for path, expected in (("a.txt", source_a), ("b.txt", source_b)):
            with self.subTest(path=path):
                pages = await self.diff_pages(None, head, path)
                self.assertGreater(len(pages), 100)
                self.assertEqual({page["representation"] for page in pages}, {"replacement-new"})
                self.assertEqual("".join(page["content"] for page in pages), expected)
                self.assertTrue(all(page["snapshot_id"] == manifest["snapshot"]["snapshot_id"] for page in pages))
                self.assertTrue(all((page["old_start"], page["old_end"]) == (0, 0) for page in pages))
                self.assertTrue(all((page["new_start"], page["new_end"]) == (1, 1) for page in pages))
        self.assertEqual(len(self.objects.requests), 4)
        self.assertLessEqual(self.client._changes.cache.bytes, changes.MAX_CHANGE_CACHE_BYTES)

    async def test_large_modified_file_replacement_old_and_new_are_both_complete(self):
        old_text = ("old line " + "a" * 80 + "\n") * 3500
        new_text = ("new line " + "b" * 80 + "\n") * 3500
        base = self.objects.commit(self.objects.tree({"large.txt": self.objects.file(old_text)}))
        head = self.objects.commit(self.objects.tree({"large.txt": self.objects.file(new_text)}), [base])
        pages = await self.diff_pages(base, head, "large.txt")
        self.assertEqual("".join(page["content"] for page in pages if page["representation"] == "replacement-old"), old_text)
        self.assertEqual("".join(page["content"] for page in pages if page["representation"] == "replacement-new"), new_text)
        old_pages = [page for page in pages if page["representation"] == "replacement-old"]
        new_pages = [page for page in pages if page["representation"] == "replacement-new"]
        self.assertFalse(old_pages[-1]["complete"])
        self.assertEqual(old_pages[0]["old_start"], 1)
        self.assertEqual(old_pages[-1]["old_end"], 3500)
        self.assertEqual(new_pages[0]["new_start"], 1)
        self.assertEqual(new_pages[-1]["new_end"], 3500)
        self.assertEqual(len(self.objects.requests), 6)

    async def test_unified_fragments_reconstruct_exact_diff_with_no_newline(self):
        old_text = "context\n" + 'old "' + "\\" * 2500
        new_text = "context\n" + 'new "' + "\\" * 2500
        base = self.objects.commit(self.objects.tree({"code.txt": self.objects.file(old_text)}))
        head = self.objects.commit(self.objects.tree({"code.txt": self.objects.file(new_text)}), [base])
        large = await self.client.read_diff_chunk(REPOSITORY, base, head, "code.txt", max_bytes=32768)
        self.assertTrue(large["complete"])
        pages = await self.diff_pages(base, head, "code.txt", max_bytes=1024)
        self.assertEqual("".join(page["content"] for page in pages), large["content"])
        self.assertEqual({page["representation"] for page in pages}, {"unified"})
        self.assertIn("\\ No newline at end of file", large["content"])
        self.assertIn("-old", large["content"])
        self.assertIn("+new", large["content"])
        self.assertTrue(any(page["old_start"] == page["old_end"] == 2 for page in pages))

    async def test_unicode_escapes_long_line_source_pages_preserve_exact_requested_range(self):
        giant = '中😀"\\\t' * 15_000 + "\n"
        text = "before\n" + giant + "after\nlast"
        head = self.objects.commit(self.objects.tree({"unicode.txt": self.objects.file(text)}))
        pages, cursor = [], None
        while True:
            page = await self.client.read_file_range(
                REPOSITORY, head, "unicode.txt", start_line=2, end_line=3, cursor=cursor, max_bytes=1024,
            )
            self.assertLessEqual(changes.serialized_size(page), 1024)
            self.assertEqual(page["start_line"], 2 if not pages else pages[-1]["end_line"])
            pages.append(page)
            cursor = page["next_cursor"]
            if page["complete"]:
                break
        self.assertEqual("".join(page["content"] for page in pages), giant + "after\n")
        self.assertEqual(pages[-1]["end_line"], 3)
        self.assertEqual(len(self.objects.requests), 3)
        beyond = await self.client.read_file_range(REPOSITORY, head, "unicode.txt", start_line=100, end_line=120)
        self.assertEqual((beyond["content"], beyond["start_line"], beyond["end_line"]), ("", 0, 0))

    async def test_cursors_are_portable_and_bind_repository_path_and_full_snapshot(self):
        blob = self.objects.file("a" * 10_000)
        head = self.objects.commit(self.objects.tree({"a.txt": blob, "b.txt": blob}))
        first = await self.client.read_diff_chunk(REPOSITORY, None, head, "a.txt", max_bytes=1024)
        resumed = await self.objects.client().read_diff_chunk(
            REPOSITORY, None, head, "a.txt", cursor=first["next_cursor"], max_bytes=1024,
        )
        repeat = await self.client.read_diff_chunk(
            REPOSITORY, None, head, "a.txt", cursor=first["next_cursor"], max_bytes=1024,
        )
        self.assertEqual(resumed, repeat)
        self.assertNotEqual(first["chunk_id"], repeat["chunk_id"])
        for repository, base, target_head, path in (
            ("owner/other", None, head, "a.txt"),
            (REPOSITORY, None, head, "b.txt"),
            (REPOSITORY, None, oid("another head"), "a.txt"),
            (REPOSITORY, head, head, "a.txt"),
        ):
            with self.subTest(repository=repository, path=path, base=base):
                before = len(self.objects.provider.calls)
                with self.assertRaisesRegex(GitHubAppError, "cursor"):
                    await self.client.read_diff_chunk(repository, base, target_head, path, cursor=first["next_cursor"])
                self.assertEqual(len(self.objects.provider.calls), before)
        damaged = first["next_cursor"][:-1] + ("0" if first["next_cursor"][-1] != "0" else "1")
        with self.assertRaisesRegex(GitHubAppError, "cursor"):
            await self.client.read_diff_chunk(REPOSITORY, None, head, "a.txt", cursor=damaged)
        request_id = changes._identity("diff-request", first["snapshot_id"], "a.txt")
        decoded = changes._page_cursor(first["next_cursor"], "d", request_id)
        for replacement in ({"i": "0" * 64}, {"o": 999_999}, {"p": 2}):
            forged = changes._encode_cursor({**decoded, **replacement})
            with self.assertRaisesRegex(GitHubAppError, "cursor"):
                await self.client.read_diff_chunk(REPOSITORY, None, head, "a.txt", cursor=forged)

    async def test_inventory_cursors_validate_original_selector_parent_and_offsets(self):
        entry = self.objects.file("text")
        head = self.objects.commit(self.objects.tree({"a": entry, "b": entry}))
        first = await self.client.list_change_files(REPOSITORY, commit_sha=head, per_page=1)
        for arguments in (
            {"repository": "owner/other", "commit_sha": head},
            {"repository": REPOSITORY, "commit_sha": oid("different")},
            {"repository": REPOSITORY, "pull_number": 7},
        ):
            before = len(self.objects.provider.calls)
            with self.assertRaisesRegex(GitHubAppError, "cursor"):
                await self.client.list_change_files(**arguments, cursor=first["next_cursor"])
            self.assertEqual(len(self.objects.provider.calls), before)
        decoded = changes._decode_cursor(first["next_cursor"], "l", {"r", "t", "b", "h", "a", "o", "s"})
        for replacement in ({"s": "0" * 64}, {"o": 100}, {"o": True}):
            with self.assertRaises(GitHubAppError):
                await self.client.list_change_files(
                    REPOSITORY, commit_sha=head, cursor=changes._encode_cursor({**decoded, **replacement}),
                )
        false_base = oid("false parent")
        forged = changes._encode_cursor({
            **decoded, "b": false_base, "s": changes.snapshot_id(REPOSITORY, false_base, head),
        })
        with self.assertRaisesRegex(GitHubAppError, "first parent"):
            await self.client.list_change_files(REPOSITORY, commit_sha=head, cursor=forged)

    async def test_file_cursors_bind_requested_range(self):
        head = self.objects.commit(self.objects.tree({"a": self.objects.file("x" * 10_000 + "\nsecond\n")}))
        first = await self.client.read_file_range(REPOSITORY, head, "a", end_line=1, max_bytes=1024)
        before = len(self.objects.provider.calls)
        with self.assertRaisesRegex(GitHubAppError, "cursor"):
            await self.client.read_file_range(REPOSITORY, head, "a", end_line=2, cursor=first["next_cursor"])
        self.assertEqual(len(self.objects.provider.calls), before)

    async def test_binary_non_utf8_symlink_submodule_and_large_blob_dispositions(self):
        files = {
            "binary": self.objects.file(b"x\0y"),
            "encoding": self.objects.file(b"\xff"),
            "link": self.objects.file("../private", mode="120000"),
            "module": {"sha": oid("gitlink"), "mode": "160000", "type": "commit"},
            "huge": {"sha": oid("huge"), "mode": "100644", "type": "blob", "size": changes.MAX_CHANGE_BLOB_BYTES + 1},
        }
        head = self.objects.commit(self.objects.tree(files))
        for path, status, reason in (
            ("binary", "binary", "binary"),
            ("encoding", "unsupported", "non_utf8"),
            ("link", "unsupported", "symlink"),
            ("module", "unsupported", "submodule"),
            ("huge", "unsupported", "blob_too_large"),
        ):
            with self.subTest(path=path):
                diff = await self.client.read_diff_chunk(REPOSITORY, None, head, path)
                source = await self.client.read_file_range(REPOSITORY, head, path)
                for result in (diff, source):
                    self.assertEqual(result["status"], status)
                    self.assertEqual(result["reason"], reason)
                    self.assertTrue(result["complete"])
                    self.assertEqual(result["content"], "")
                    self.assertIsNone(result["next_cursor"])
        for path in ("link", "module", "huge"):
            self.assertNotIn(f"/git/blobs/{files[path]['sha']}", self.objects.routes())
        with self.assertRaisesRegex(GitHubAppError, "never followed"):
            await self.client.read_file_range(REPOSITORY, head, "link/secret")

    async def test_input_errors_happen_before_authentication(self):
        sha = "a" * 40
        calls = [
            ("list_change_files", {"repository": None, "commit_sha": sha}),
            ("list_change_files", {"repository": "owner/..", "commit_sha": sha}),
            ("list_change_files", {"repository": REPOSITORY}),
            ("list_change_files", {"repository": REPOSITORY, "pull_number": True}),
            ("list_change_files", {"repository": REPOSITORY, "commit_sha": "a" * 7}),
            ("list_change_files", {"repository": REPOSITORY, "commit_sha": sha, "head_sha": sha}),
            ("list_change_files", {"repository": REPOSITORY, "head_sha": sha}),
            ("list_change_files", {"repository": REPOSITORY, "commit_sha": sha, "per_page": 101}),
            ("list_change_files", {"repository": REPOSITORY, "commit_sha": sha, "cursor": "forged"}),
        ]
        for path in ("../file", "/file", "a/./b", "a//b", "a\\b", "a\nb", "a\0b", "", "x" * 241):
            calls.append(("read_diff_chunk", {"repository": REPOSITORY, "base_sha": None, "head_sha": sha, "path": path}))
        for size in (1023, 32769, True, "4096", None):
            calls.append(("read_diff_chunk", {"repository": REPOSITORY, "base_sha": None, "head_sha": sha, "path": "x", "max_bytes": size}))
        for start, end in ((0, 1), (3, 2), (1, 10001), (True, 2)):
            calls.append(("read_file_range", {"repository": REPOSITORY, "sha": sha, "path": "x", "start_line": start, "end_line": end}))
        calls.append(("read_file_range", {"repository": REPOSITORY, "sha": "main", "path": "x"}))
        for name, arguments in calls:
            with self.subTest(tool=name, arguments=arguments):
                with self.assertRaises(GitHubAppError):
                    await getattr(self.client, name)(**arguments)
        self.assertEqual(self.objects.provider.calls, [])
        self.assertEqual(self.objects.requests, [])

    async def test_truncated_and_malformed_trees_never_report_complete(self):
        tree = self.objects.tree({"a": self.objects.file("a")})
        head = self.objects.commit(tree)
        original = self.objects.payloads[f"/git/trees/{tree}"]
        for replacement in (
            {**original, "truncated": True},
            {**original, "truncated": "false"},
            {**original, "sha": "b" * 40},
            {**original, "tree": [*original["tree"], *original["tree"]]},
            {**original, "tree": [{**original["tree"][0], "path": "a/b"}]},
            {**original, "tree": [{**original["tree"][0], "mode": []}]},
        ):
            self.objects.payloads[f"/git/trees/{tree}"] = replacement
            with self.assertRaises(GitHubAppError):
                await self.objects.client().list_change_files(REPOSITORY, commit_sha=head)

    async def test_blob_identity_size_base64_and_encoding_are_checked(self):
        entry = self.objects.file("hello")
        head = self.objects.commit(self.objects.tree({"a": entry}))
        route = f"/git/blobs/{entry['sha']}"
        original = self.objects.payloads[route]
        for replacement in (
            {**original, "sha": "b" * 40},
            {**original, "size": 4},
            {**original, "content": "!"},
            {**original, "content": base64.b64encode(b"short").decode()},
            {**original, "content": base64.b64encode(b"helloo").decode()},
        ):
            self.objects.payloads[route] = replacement
            with self.assertRaises(GitHubAppError):
                await self.objects.client().read_file_range(REPOSITORY, head, "a")
        self.objects.payloads[route] = {**original, "encoding": "unsupported"}
        result = await self.objects.client().read_file_range(REPOSITORY, head, "a")
        self.assertEqual((result["status"], result["reason"]), ("unsupported", "unsupported_encoding"))

    async def test_bounded_public_fallback_cannot_reuse_private_cached_data(self):
        head = self.objects.commit(self.objects.tree({"a": self.objects.file("source")}))
        await self.client.read_file_range(REPOSITORY, head, "a")
        self.objects.provider.available = False
        public = await self.client.read_file_range(REPOSITORY, head, "a")
        self.assertEqual(public["content"], "source")
        self.assertNotIn("authorization", self.objects.requests[-1].headers)
        self.objects.public = False
        with self.assertRaisesRegex(GitHubAppError, "publicly readable"):
            await self.client.read_file_range(REPOSITORY, head, "a")
        self.objects.overrides[""] = httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
        with self.assertRaisesRegex(GitHubAppError, "rate limit"):
            await self.client.read_file_range(REPOSITORY, head, "a")

    async def test_bad_credentials_are_rejected_before_cache_or_transport(self):
        head = self.objects.commit(self.objects.tree({"a": self.objects.file("source")}))
        for field, value in (
            ("repository_override", "outside/repo"),
            ("permissions_override", (("contents", "write"),)),
            ("expires_at", 0),
        ):
            provider = Provider()
            setattr(provider, field, value)
            client = GitHubClient(provider, transport=httpx.MockTransport(self.objects.handler))
            with self.assertRaisesRegex(GitHubAppError, "credential scope or expiry"):
                await client.read_file_range(REPOSITORY, head, "a")
        self.assertEqual(self.objects.requests, [])

    async def test_no_redirects_or_arbitrary_routes_and_errors_do_not_echo_payloads(self):
        head = self.objects.commit(self.objects.tree({}))
        self.objects.overrides[f"/git/commits/{head}"] = httpx.Response(
            302, headers={"Location": "https://example.invalid/private"},
        )
        with self.assertRaisesRegex(GitHubAppError, "HTTP 302"):
            await self.client.list_change_files(REPOSITORY, commit_sha=head)
        self.assertEqual(len(self.objects.requests), 1)
        session = changes._Session(self.client._changes, REPOSITORY)
        try:
            for route in ("https://example.invalid", "/git/blobs/main", "/compare/a...b", "/git/trees/" + "a" * 40 + "?recursive=1"):
                with self.assertRaisesRegex(GitHubAppError, "Unsupported"):
                    await session.get(route)
        finally:
            await session.client.aclose()
        self.assertEqual(len(self.objects.requests), 1)

    async def test_tree_request_byte_traversal_and_session_limits_are_explicit(self):
        head = self.objects.commit(self.objects.tree({str(index): self.objects.file("x") for index in range(4)}))
        for name, value, message in (
            ("MAX_CHANGE_FILES", 3, "inventory budget"),
            ("MAX_CHANGE_TREE_ENTRIES", 3, "tree entry budget"),
            ("MAX_CHANGE_OPERATION_REQUESTS", 1, "request budget"),
            ("MAX_CHANGE_OPERATION_BYTES", 20, "download byte budget"),
            ("MAX_CHANGE_SESSION_BYTES", 20, "download byte budget"),
            ("MAX_CHANGE_SESSION_REQUESTS", 1, "request budget"),
            ("MAX_CHANGE_OBJECT_HTTP_BYTES", 100, "endpoint byte limit"),
        ):
            with self.subTest(limit=name):
                with patch.object(changes, name, value):
                    with self.assertRaisesRegex(GitHubAppError, message):
                        await self.objects.client().list_change_files(REPOSITORY, commit_sha=head)
        nested = self.objects.tree({"leaf": self.objects.file("text")})
        for _ in range(3):
            nested = self.objects.tree({"dir": self.objects.directory(nested)})
        deep = self.objects.commit(nested)
        with patch.object(changes, "MAX_CHANGE_DEPTH", 2):
            with self.assertRaisesRegex(GitHubAppError, "depth budget"):
                await self.objects.client().list_change_files(REPOSITORY, commit_sha=deep)

    async def test_streamed_object_overflow_stops_before_json_and_closes_response(self):
        entry = self.objects.file("hello")
        head = self.objects.commit(self.objects.tree({"a": entry}))
        consumed, closed = [], []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(20):
                    consumed.append(1)
                    yield b"x" * 65_536

            async def aclose(self):
                closed.append(True)

        self.objects.overrides[f"/git/blobs/{entry['sha']}"] = lambda _: httpx.Response(200, stream=Stream())
        with patch.object(changes, "MAX_CHANGE_OBJECT_HTTP_BYTES", 131_072):
            with self.assertRaisesRegex(GitHubAppError, "endpoint byte limit"):
                await self.client.read_file_range(REPOSITORY, head, "a")
        self.assertEqual(len(consumed), 3)
        self.assertEqual(closed, [True])

    async def test_cache_eviction_ttl_and_disabled_cache_do_not_change_correctness(self):
        head = self.objects.commit(self.objects.tree({"a": self.objects.file("x" * 20_000)}))
        first = await self.client.read_diff_chunk(REPOSITORY, None, head, "a", max_bytes=1024)
        cache = self.client._changes.cache
        expected = await self.client.read_diff_chunk(
            REPOSITORY, None, head, "a", cursor=first["next_cursor"], max_bytes=1024,
        )
        with patch.object(changes, "MAX_CHANGE_CACHE_BYTES", 4096), patch.object(changes, "MAX_CHANGE_CACHE_ENTRIES", 2):
            for index in range(10):
                cache.put((REPOSITORY, "test", oid(str(index))), "x" * 512)
                self.assertLessEqual(cache.bytes, 4096)
                self.assertLessEqual(len(cache.items), 2)
        with patch.object(changes, "CHANGE_CACHE_TTL_SECONDS", 0):
            self.assertIsNone(cache.get((REPOSITORY, "test", oid("9"))))
            self.assertEqual(cache.bytes, 0)
        actual = await self.client.read_diff_chunk(
            REPOSITORY, None, head, "a", cursor=first["next_cursor"], max_bytes=1024,
        )
        self.assertEqual(actual, expected)
        with patch.object(changes, "MAX_CHANGE_CACHE_BYTES", 0):
            client = self.objects.client()
            uncached = await client.read_diff_chunk(REPOSITORY, None, head, "a", max_bytes=1024)
            self.assertEqual(uncached, first)
            self.assertEqual(client._changes.cache.bytes, 0)
        self.assertNotIn("test-app-credential", repr(cache.items))

    async def test_cancellation_and_cooperative_deadline_release_client_and_lock(self):
        started = asyncio.Event()
        closed = []

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.append(True)

        client = GitHubClient(Provider(), transport=Transport())
        task = asyncio.create_task(client.list_change_files(REPOSITORY, commit_sha="a" * 40))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(client._changes.lock.locked())
        self.assertEqual(closed, [True])
        with patch.object(changes, "MAX_CHANGE_SECONDS", 0.01):
            with self.assertRaisesRegex(GitHubAppError, "cooperative time budget"):
                await client.list_change_files(REPOSITORY, commit_sha="a" * 40)
        self.assertFalse(client._changes.lock.locked())
        self.assertEqual(len(closed), 2)

    async def test_cooperative_time_check_includes_lock_wait_before_sync_work(self):
        elapsed = [0.0]

        class DelayedLock:
            async def __aenter__(self):
                elapsed[0] = 50.0

            async def __aexit__(self, *args):
                return False

        head = self.objects.commit(self.objects.tree({}))

        def slow_response(request):
            elapsed[0] += 11.0
            return httpx.Response(200, json=self.objects.payloads[f"/git/commits/{head}"])

        self.objects.overrides[f"/git/commits/{head}"] = slow_response
        self.client._changes.lock = DelayedLock()
        clock = SimpleNamespace(monotonic=lambda: elapsed[0], time=time.time)
        with patch.object(changes, "time", clock):
            with self.assertRaisesRegex(GitHubAppError, "cooperative time budget"):
                await self.client.list_change_files(REPOSITORY, commit_sha=head)

    async def test_real_mcp_discovery_strict_validation_and_bounded_calls(self):
        head = self.objects.commit(self.objects.tree({"source.txt": self.objects.file('中"\\' * 2000)}))
        async with Client(create_server(self.client)) as mcp:
            tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
            for name in ("list_change_files", "read_diff_chunk", "read_file_range"):
                self.assertTrue(tools[name].description)
                self.assertTrue(tools[name].annotations.read_only_hint)
                self.assertFalse(tools[name].annotations.destructive_hint)
            diff_schema = tools["read_diff_chunk"].input_schema
            self.assertEqual(diff_schema["properties"]["max_bytes"]["minimum"], 1024)
            self.assertEqual(diff_schema["properties"]["max_bytes"]["maximum"], 32768)
            self.assertIn("base_sha", diff_schema["required"])
            self.assertEqual(diff_schema["properties"]["head_sha"]["pattern"], r"^[0-9a-fA-F]{40}$")
            for tool, arguments in (
                ("list_change_files", {"repository": REPOSITORY, "commit_sha": head, "per_page": True}),
                ("read_file_range", {"repository": REPOSITORY, "sha": head, "path": "source.txt", "start_line": "1"}),
                ("read_diff_chunk", {"repository": REPOSITORY, "base_sha": None, "head_sha": head, "path": "source.txt", "max_bytes": "1024"}),
            ):
                invalid = await mcp.call_tool(tool, arguments)
                self.assertTrue(invalid.is_error)
            self.assertEqual(self.objects.provider.calls, [])
            manifest = await mcp.call_tool("list_change_files", {"repository": REPOSITORY, "commit_sha": head})
            self.assertFalse(manifest.is_error)
            pinned = json.loads(manifest.content[0].text)["snapshot"]
            for tool, arguments in (
                ("read_diff_chunk", {"base_sha": None, "head_sha": head}),
                ("read_file_range", {"sha": head}),
            ):
                result = await mcp.call_tool(tool, {
                    "repository": REPOSITORY, "path": "source.txt", "max_bytes": 1024, **arguments,
                })
                self.assertFalse(result.is_error)
                text = result.content[0].text
                self.assertLessEqual(len(text.encode("utf-8")), 1024)
                value = json.loads(text)
                self.assertLessEqual(changes.serialized_size(value), 1024)
                self.assertFalse(value["complete"])
                if tool == "read_diff_chunk":
                    self.assertEqual(value["snapshot_id"], pinned["snapshot_id"])


class SearchObjectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.objects = Objects()
        self.client = self.objects.client()
        self.head = self.objects.commit(self.objects.tree({"source.txt": self.objects.file("needle\n")}))
        logger = logging.getLogger("httpx")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)

    async def test_pinned_search_avoids_oversized_rest_commit_patches(self):
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await self.client.get_commit(REPOSITORY, self.head)
        self.objects.requests.clear()
        self.objects.provider.calls.clear()
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=self.head)
        self.assertEqual(result["resolved_ref"], self.head)
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["items"][0]["path"], "source.txt")
        self.assertEqual(len(self.objects.requests), 3)
        self.assertEqual(self.objects.routes()[0], f"/git/commits/{self.head}")
        self.assertEqual(self.objects.provider.calls, [(REPOSITORY, {"contents": "read"})])
        self.assertFalse(any(route.startswith("/commits/") for route in self.objects.routes()))

    async def test_branch_resolution_is_fixed_route_then_immutable_git_commit(self):
        self.objects.payloads["/git/ref/heads/release/topic"] = {
            "ref": "refs/heads/release/topic",
            "object": {"sha": self.head, "type": "commit", "url": "https://outside.invalid/ignored"},
        }
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref="release/topic")
        self.assertEqual(result["resolved_ref"], self.head)
        self.assertEqual(self.objects.requests[0].url.raw_path, b"/repos/owner/repo/git/ref/heads/release%2Ftopic")
        self.assertEqual(self.objects.routes()[1], f"/git/commits/{self.head}")
        self.assertEqual(len(self.objects.provider.calls), 1)
        self.assertFalse(result["incomplete_results"])

    async def test_lightweight_and_annotated_tags_are_peeled_within_bound(self):
        first, second = oid("first tag"), oid("second tag")
        self.objects.payloads["/git/ref/tags/v1"] = {
            "ref": "refs/tags/v1", "object": {"type": "tag", "sha": first},
        }
        self.objects.payloads[f"/git/tags/{first}"] = {
            "sha": first, "object": {"type": "tag", "sha": second},
        }
        self.objects.payloads[f"/git/tags/{second}"] = {
            "sha": second, "object": {"type": "commit", "sha": self.head},
        }
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref="v1")
        self.assertEqual(result["resolved_ref"], self.head)
        self.assertEqual(self.objects.routes()[:5], [
            "/git/ref/heads/v1", "/git/ref/tags/v1", f"/git/tags/{first}",
            f"/git/tags/{second}", f"/git/commits/{self.head}",
        ])
        self.objects.payloads["/git/ref/tags/v2"] = {
            "ref": "refs/tags/v2", "object": {"type": "commit", "sha": self.head},
        }
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref="refs/tags/v2")
        self.assertEqual(result["resolved_ref"], self.head)
        self.assertNotIn("/git/ref/heads/refs/tags/v2", self.objects.routes())

    async def test_legacy_abbreviated_sha_uses_one_metadata_item_then_full_git_object(self):
        self.objects.payloads["/commits"] = [{"sha": self.head, "commit": {"tree": {"sha": oid("not-used")}}}]
        short = self.head[:9]
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=short)
        self.assertEqual(result["resolved_ref"], self.head)
        self.assertEqual(self.objects.routes()[:4], [
            f"/git/ref/heads/{short}", f"/git/ref/tags/{short}", "/commits", f"/git/commits/{self.head}",
        ])
        self.assertEqual(dict(self.objects.requests[2].url.params), {"sha": short, "per_page": "1"})
        self.assertEqual(len(self.objects.provider.calls), 1)
        self.objects.payloads["/commits"] = [{"sha": oid("unrelated commit")}]
        with self.assertRaisesRegex(GitHubAppError, "different search commit"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=short)

    async def test_head_uses_default_branch_and_tag_depth_is_bounded(self):
        self.objects.overrides[""] = httpx.Response(200, json={"default_branch": "main"})
        self.objects.payloads["/git/ref/heads/main"] = {
            "ref": "refs/heads/main", "object": {"sha": self.head, "type": "commit"},
        }
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref="HEAD")
        self.assertEqual(result["resolved_ref"], self.head)
        self.assertEqual(self.objects.routes()[:3], ["", "/git/ref/heads/main", f"/git/commits/{self.head}"])
        tags = [oid(f"tag-{index}") for index in range(10)]
        self.objects.payloads["/git/ref/tags/deep"] = {
            "ref": "refs/tags/deep", "object": {"type": "tag", "sha": tags[0]},
        }
        for first, second in zip(tags, tags[1:]):
            self.objects.payloads[f"/git/tags/{first}"] = {
                "sha": first, "object": {"type": "tag", "sha": second},
            }
        before = len(self.objects.requests)
        with self.assertRaisesRegex(GitHubAppError, "tag limit"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref="refs/tags/deep")
        self.assertEqual(len(self.objects.requests) - before, 9)

    async def test_unknown_refs_bad_ref_identity_and_tag_cycles_fail_without_indexed_fallback(self):
        with self.assertRaises(GitHubAppError):
            await self.client.search_repository_content(REPOSITORY, "needle", ref="missing")
        self.objects.payloads["/git/ref/heads/main"] = {
            "ref": "refs/heads/other", "object": {"type": "commit", "sha": self.head},
        }
        with self.assertRaisesRegex(GitHubAppError, "invalid search ref"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref="main")
        tag = oid("tag")
        self.objects.payloads["/git/ref/tags/cycle"] = {
            "ref": "refs/tags/cycle", "object": {"type": "tag", "sha": tag},
        }
        self.objects.payloads[f"/git/tags/{tag}"] = {
            "sha": tag, "object": {"type": "tag", "sha": tag},
        }
        with self.assertRaisesRegex(GitHubAppError, "tag limit"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref="refs/tags/cycle")
        self.assertFalse(any("search" in route or route.startswith("/commits/") for route in self.objects.routes()))

    async def test_commit_metadata_does_not_get_the_large_object_http_allowance(self):
        self.objects.payloads[f"/git/commits/{self.head}"]["message"] = "x" * 400_000
        with self.assertRaisesRegex(GitHubAppError, "endpoint byte limit"):
            await self.client.list_change_files(REPOSITORY, commit_sha=self.head)
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=self.head)


if __name__ == "__main__":
    unittest.main()
