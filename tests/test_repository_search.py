import base64
import copy
import hashlib
import json
import unittest

import httpx

from github_app_mcp.src.issuelens_github_mcp.auth import GitHubAppError, InstallationCredential
from github_app_mcp.src.issuelens_github_mcp.github import GitHubClient


REPOSITORY = "microsoft/IssueLens"
OLD_COMMIT = "a" * 40
NEW_COMMIT = "b" * 40
OLD_TREE = "c" * 40
NEW_TREE = "d" * 40


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def get_token(self, repository, permissions):
        self.calls.append((repository, permissions))
        return InstallationCredential(
            installation_id=1234,
            repository=repository,
            permissions=tuple(sorted(permissions.items())),
            token="fake-repository-token",
            expires_at=float("inf"),
        )


class RepositorySearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.provider = FakeProvider()
        self.overrides = {}
        self.advance_ref_on_resolve = False
        self.blobs = {}
        self.trees = {
            OLD_TREE: {"sha": OLD_TREE, "truncated": False, "tree": []},
            NEW_TREE: {"sha": NEW_TREE, "truncated": False, "tree": []},
        }
        self.refs = {"release/topic": OLD_COMMIT, "main": NEW_COMMIT}
        self.commits = {
            OLD_COMMIT: {"sha": OLD_COMMIT, "commit": {"tree": {"sha": OLD_TREE}}},
            NEW_COMMIT: {"sha": NEW_COMMIT, "commit": {"tree": {"sha": NEW_TREE}}},
        }
        self.indexed = {"items": [{"path": "current-main.txt"}], "total_count": 1}
        self.client = GitHubClient(
            self.provider, transport=httpx.MockTransport(self.handler)
        )

    def add_file(self, path, content, *, tree=OLD_TREE, mode="100644"):
        blob_sha = hashlib.sha1(
            b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
        ).hexdigest()
        self.trees[tree]["tree"].append({
            "path": path, "type": "blob", "mode": mode,
            "sha": blob_sha, "size": len(content),
        })
        self.blobs[blob_sha] = {
            "sha": blob_sha, "size": len(content), "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }
        return blob_sha

    def handler(self, request):
        self.requests.append(request)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "api.github.com")
        if request.url.path == "/search/code":
            return httpx.Response(200, json=self.indexed)
        prefix = f"/repos/{REPOSITORY}/"
        self.assertTrue(request.url.path.casefold().startswith(prefix.casefold()))
        route = request.url.path[len(prefix):]
        if route in self.overrides:
            return self.overrides[route]
        if route.startswith("commits/"):
            ref = route.removeprefix("commits/")
            commit = self.commits.get(self.refs.get(ref, ref).lower())
            if self.advance_ref_on_resolve:
                self.refs["release/topic"] = NEW_COMMIT
            return httpx.Response(200, json=commit) if commit is not None else httpx.Response(404)
        if route.startswith("git/trees/"):
            self.assertEqual(request.url.params["recursive"], "1")
            return httpx.Response(200, json=self.trees[route.removeprefix("git/trees/")])
        if route.startswith("git/blobs/"):
            return httpx.Response(200, json=self.blobs[route.removeprefix("git/blobs/")])
        self.fail(f"Unexpected request: {request.url}")

    async def test_ref_search_uses_only_requested_snapshot(self):
        old_blob = self.add_file("src/old.py", b"old implementation\nNEEDLE from release\n")
        self.add_file("src/new.py", b"needle from current main", tree=NEW_TREE)
        self.advance_ref_on_resolve = True

        result = await self.client.search_repository_content(
            REPOSITORY, "needle", ref="release/topic"
        )

        self.assertEqual(result["resolved_ref"], OLD_COMMIT)
        self.assertEqual(result["total_count"], 1)
        self.assertFalse(result["incomplete_results"])
        self.assertEqual(result["skipped_files"], 0)
        self.assertEqual([item["path"] for item in result["items"]], ["src/old.py"])
        self.assertEqual(result["items"][0]["sha"], old_blob)
        self.assertEqual(result["items"][0]["repository"], {"full_name": REPOSITORY})
        self.assertEqual(
            result["items"][0]["html_url"],
            f"https://github.com/{REPOSITORY}/blob/{OLD_COMMIT}/src/old.py",
        )
        self.assertEqual(result["items"][0]["matches"][0]["line_number"], 2)
        self.assertEqual(
            [request.url.path for request in self.requests],
            [f"/repos/{REPOSITORY}/commits/release/topic",
             f"/repos/{REPOSITORY}/git/trees/{OLD_TREE}",
             f"/repos/{REPOSITORY}/git/blobs/{old_blob}"],
        )
        self.assertIn(b"release%2Ftopic", self.requests[0].url.raw_path)
        self.assertTrue(all(
            call == (REPOSITORY, {"contents": "read"}) for call in self.provider.calls
        ))

    async def test_no_ref_preserves_indexed_query(self):
        result = await self.client.search_repository_content(
            REPOSITORY, "needle", per_page=5, page=2
        )

        self.assertEqual(result, self.indexed)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/search/code")
        self.assertEqual(self.requests[0].url.params["q"], f"needle repo:{REPOSITORY}")
        self.assertEqual(self.requests[0].url.params["per_page"], "5")
        self.assertEqual(self.requests[0].url.params["page"], "2")

    async def test_invalid_queries_are_rejected_before_authentication(self):
        queries = (
            None, 123, [], {}, "", " " * 10, "a" * 513,
            "needle repo:other/project", "org:other", "path:secret", "ref:main",
            "needle\n", "needle\t", "needle\x00", "needle\x7f",
            "needle\u200b", "needle\u0085", "needle\u2028",
        )
        for ref in (None, "main"):
            for query in queries:
                with self.subTest(query=query, ref=ref):
                    with self.assertRaises(GitHubAppError):
                        await self.client.search_repository_content(REPOSITORY, query, ref=ref)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.provider.calls, [])

    async def test_invalid_refs_and_repository_are_rejected_before_authentication(self):
        for ref in ("", 123, [], "main\n", "main:other", "../main", "HEAD~1", "-main", "main?repo=other"):
            with self.subTest(ref=ref):
                with self.assertRaises(GitHubAppError):
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=ref)
        with self.assertRaises(GitHubAppError):
            await self.client.search_repository_content("microsoft/IssueLens/other", "needle", ref="main")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.provider.calls, [])

    async def test_invalid_pagination_is_rejected_before_authentication(self):
        for ref in (None, "main"):
            for pagination in (
                {"page": 0}, {"page": 101}, {"page": True}, {"page": "1"},
                {"per_page": 0}, {"per_page": 101}, {"per_page": False},
            ):
                with self.subTest(ref=ref, pagination=pagination):
                    with self.assertRaises(GitHubAppError):
                        await self.client.search_repository_content(REPOSITORY, "needle", ref=ref, **pagination)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.provider.calls, [])

    async def test_pin_failures_never_fall_back_to_index(self):
        for status in (301, 403, 404, 422, 500):
            with self.subTest(status=status):
                self.requests.clear()
                self.overrides["commits/release/topic"] = httpx.Response(
                    status, text="secret-response-sentinel", headers={"Location": "https://other.example"}
                )
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref="release/topic")
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 1)
                self.assertNotEqual(self.requests[0].url.path, "/search/code")

    async def test_pagination_reuses_resolved_commit_after_branch_moves(self):
        self.add_file("z.txt", b"needle old last")
        self.add_file("a.txt", b"needle old first")
        self.add_file("new.txt", b"needle new snapshot", tree=NEW_TREE)
        first = await self.client.search_repository_content(REPOSITORY, "needle", ref="release/topic", per_page=1)
        self.refs["release/topic"] = NEW_COMMIT
        second = await self.client.search_repository_content(
            REPOSITORY, "needle", ref=first["resolved_ref"], per_page=1, page=2
        )
        repeat = await self.client.search_repository_content(
            REPOSITORY, "needle", ref=first["resolved_ref"], per_page=1
        )
        past_end = await self.client.search_repository_content(
            REPOSITORY, "needle", ref=first["resolved_ref"], per_page=1, page=3
        )
        moved = await self.client.search_repository_content(REPOSITORY, "needle", ref="release/topic")

        self.assertEqual(first, repeat)
        self.assertEqual(first["total_count"], 2)
        self.assertFalse(first["incomplete_results"])
        self.assertEqual(first["items"][0]["path"], "a.txt")
        self.assertEqual(second["items"][0]["path"], "z.txt")
        self.assertEqual(second["resolved_ref"], OLD_COMMIT)
        self.assertEqual(past_end["items"], [])
        self.assertEqual(past_end["total_count"], 2)
        self.assertEqual(moved["resolved_ref"], NEW_COMMIT)
        self.assertEqual([item["path"] for item in moved["items"]], ["new.txt"])

    async def test_literal_content_not_paths_regex_or_search_operators(self):
        self.add_file("needle.txt", b"unrelated content")
        self.add_file("literal.txt", "Stra\u00dfe a.*b OR literal\n".encode("utf-8"))
        for query in ("STRASSE", "a.*b OR literal"):
            with self.subTest(query=query):
                result = await self.client.search_repository_content(REPOSITORY, query, ref=OLD_COMMIT)
                self.assertEqual([item["path"] for item in result["items"]], ["literal.txt"])
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(result["total_count"], 0)
        self.assertFalse(result["incomplete_results"])

    async def test_case_preserving_branch_and_safe_commit_pinned_url(self):
        self.refs["Release/Topic"] = OLD_COMMIT
        path = "src/space #?% \u00fc.txt"
        blob_sha = self.add_file(path, b"needle", mode="100755")
        self.commits[OLD_COMMIT]["sha"] = OLD_COMMIT.upper()
        self.trees[OLD_TREE]["sha"] = OLD_TREE.upper()
        self.blobs[blob_sha]["sha"] = blob_sha.upper()
        result = await self.client.search_repository_content(
            "Microsoft/IssueLens", "needle", ref="Release/Topic"
        )
        self.assertEqual(result["resolved_ref"], OLD_COMMIT)
        self.assertEqual(result["items"][0]["name"], "space #?% \u00fc.txt")
        self.assertEqual(
            result["items"][0]["html_url"],
            f"https://github.com/Microsoft/IssueLens/blob/{OLD_COMMIT}/src/space%20%23%3F%25%20%C3%BC.txt",
        )
        self.assertIn(b"Release%2FTopic", self.requests[0].url.raw_path)
        self.assertEqual(self.provider.calls[0], ("Microsoft/IssueLens", {"contents": "read"}))

    async def test_truncated_tree_fails_before_blob_reads(self):
        self.add_file("found.txt", b"needle")
        self.trees[OLD_TREE]["truncated"] = True
        with self.assertRaisesRegex(GitHubAppError, "truncated"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_file_count_limit_fails_before_blob_reads(self):
        for index in range(65):
            self.add_file(f"{index:02}.txt", b"needle")
        with self.assertRaisesRegex(GitHubAppError, "64 regular files"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_total_content_limit_fails_before_blob_reads(self):
        for index in range(5):
            self.add_file(f"{index}.txt", b"x" * (64 * 1024))
        with self.assertRaisesRegex(GitHubAppError, "262144 content bytes"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_exact_file_and_byte_limits_are_accepted(self):
        for index in range(64):
            self.add_file(f"{index:02}.txt", b"needle" + b"x" * (4096 - 6))
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=100)
        self.assertEqual(result["total_count"], 64)
        self.assertEqual(len(result["items"]), 64)
        self.assertEqual(len(self.requests), 66)
        self.assertFalse(result["incomplete_results"])

    async def test_skipped_files_are_explicit_and_never_follow_links(self):
        regular_blob = self.add_file("regular.txt", b"needle")
        self.add_file("binary.bin", b"needle\x00hidden")
        self.add_file("legacy.txt", b"needle\xff")
        unsupported = self.add_file("encoding.txt", b"needle unsupported")
        self.blobs[unsupported]["encoding"] = "rot13"
        symlink_blob = self.add_file("symlink", b"../../needle", mode="120000")
        oversized = self.add_file("huge.txt", b"needle" + b"x" * (64 * 1024))
        self.trees[OLD_TREE]["tree"].extend([
            {"path": "vendor", "type": "commit", "mode": "160000", "sha": NEW_COMMIT},
            {"path": "directory", "type": "tree", "mode": "040000", "sha": NEW_TREE},
        ])

        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual([item["sha"] for item in result["items"]], [regular_blob])
        self.assertEqual(result["total_count"], 1)
        self.assertTrue(result["incomplete_results"])
        self.assertEqual(result["skipped_files"], 6)
        self.assertEqual(result["skipped_reasons"], {
            "binary": 1, "non_utf8": 1, "unsupported_encoding": 1,
            "file_too_large": 1, "symlink": 1, "submodule": 1,
        })
        paths = [request.url.path for request in self.requests]
        self.assertNotIn(f"/repos/{REPOSITORY}/git/blobs/{symlink_blob}", paths)
        self.assertNotIn(f"/repos/{REPOSITORY}/git/blobs/{oversized}", paths)
        no_match = await self.client.search_repository_content(REPOSITORY, "absent", ref=OLD_COMMIT)
        self.assertEqual(no_match["items"], [])
        self.assertTrue(no_match["incomplete_results"])
        self.assertEqual(no_match["skipped_files"], 6)

    async def test_empty_snapshot_is_complete(self):
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total_count"], 0)
        self.assertEqual(result["skipped_reasons"], {})
        self.assertFalse(result["incomplete_results"])
        self.assertEqual(len(self.requests), 2)

    async def test_huge_matches_have_bounded_excerpts_and_line_count(self):
        self.add_file("long.txt", b"x" * (64 * 1024 - 6) + b"needle")
        self.add_file("lines.txt", b"needle\n" * 1000)
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(result["items"]), 2)
        line_matches, long_match = (item["matches"] for item in result["items"])
        self.assertEqual([match["line_number"] for match in line_matches], [1, 2, 3])
        self.assertEqual(len(long_match[0]["excerpt"]), 160)
        self.assertTrue(long_match[0]["truncated"])
        self.assertEqual(long_match[0]["line_number"], 1)
        self.assertLess(len(json.dumps(result).encode("utf-8")), 2000)

    async def test_aggregate_result_limit_and_smaller_page(self):
        content = (("needle" + "\u4e00" * 154 + "\n") * 3).encode("utf-8")
        for index in range(64):
            self.add_file(f"{index:02}.txt", content)
        with self.assertRaisesRegex(GitHubAppError, "reduce per_page"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=100)
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT, per_page=1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["total_count"], 64)
        self.assertFalse(result["incomplete_results"])

    async def test_line_numbers_count_source_newlines_not_unicode_separators(self):
        self.add_file("lines.txt", "prefix\f\u0085\u2028\r\nneedle\r\n".encode("utf-8"))
        result = await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(result["items"][0]["matches"], [
            {"line_number": 2, "excerpt": "needle", "truncated": False},
        ])

    async def test_oversized_tree_honors_request_result_limit(self):
        self.trees[OLD_TREE]["padding"] = "x" * 100_000
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
        self.assertEqual(len(self.requests), 2)

    async def test_malformed_commit_payloads_fail_safely(self):
        valid = copy.deepcopy(self.commits[OLD_COMMIT])
        for payload in (
            [], {}, {"sha": OLD_COMMIT, "commit": []},
            {"sha": OLD_COMMIT, "commit": {"tree": None}},
            {**valid, "sha": "short"}, {**valid, "sha": NEW_COMMIT},
            {**valid, "sha": [OLD_COMMIT]},
            {**valid, "commit": {"tree": {"sha": "../secret-response-sentinel"}}},
        ):
            with self.subTest(payload=payload):
                self.requests.clear()
                self.commits[OLD_COMMIT] = payload
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 1)

    async def test_malformed_tree_payloads_fail_before_blob_reads(self):
        self.add_file("file.txt", b"needle")
        valid = copy.deepcopy(self.trees[OLD_TREE])
        entry = valid["tree"][0]
        bad_entries = (
            None, {**entry, "path": "../secret-response-sentinel"},
            {**entry, "path": "a/./file.txt"}, {**entry, "path": "file\n.txt"},
            {**entry, "size": True}, {**entry, "size": -1}, {**entry, "size": "6"},
            {**entry, "mode": "100600"}, {**entry, "sha": "short"},
        )
        payloads = [
            [], {}, {**valid, "sha": NEW_TREE}, {**valid, "truncated": 1},
            {**valid, "tree": {}}, {**valid, "tree": [entry, entry]},
            {"sha": OLD_TREE, "tree": []},
            *({**valid, "tree": [bad]} for bad in bad_entries),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.requests.clear()
                self.trees[OLD_TREE] = payload
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 2)

    async def test_malformed_blob_payloads_fail_safely(self):
        blob_sha = self.add_file("file.txt", b"needle")
        valid = copy.deepcopy(self.blobs[blob_sha])
        for payload in (
            [], {}, {**valid, "sha": NEW_COMMIT}, {**valid, "size": True},
            {**valid, "size": -1}, {**valid, "size": 65537},
            {**valid, "content": []}, {**valid, "encoding": None},
            {**valid, "content": "%%%%%%%%"},
            {**valid, "content": base64.b64encode(b"short").decode("ascii")},
            {**valid, "content": "secret-response-sentinel" * 10},
        ):
            with self.subTest(payload=payload):
                self.requests.clear()
                self.blobs[blob_sha] = payload
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 3)

    async def test_blob_read_failure_and_invalid_json_do_not_fall_back(self):
        blob_sha = self.add_file("file.txt", b"needle")
        for response in (
            httpx.Response(404),
            httpx.Response(200, content=b"secret-response-sentinel is not JSON"),
            httpx.Response(200, content=b"x" * (128 * 1024 + 1)),
        ):
            with self.subTest(response=response):
                self.requests.clear()
                self.overrides[f"git/blobs/{blob_sha}"] = response
                with self.assertRaises(GitHubAppError) as raised:
                    await self.client.search_repository_content(REPOSITORY, "needle", ref=OLD_COMMIT)
                self.assertNotIn("secret-response-sentinel", str(raised.exception))
                self.assertEqual(len(self.requests), 3)
                self.assertTrue(all(request.url.path != "/search/code" for request in self.requests))
