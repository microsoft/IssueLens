from __future__ import annotations

import base64
import hashlib
import io
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

from dulwich.client import FetchPackResult, HttpGitClient, LocalGitClient
from dulwich.config import ConfigDict, StackedConfig
from dulwich.index import commit_tree
from dulwich.objects import Blob, Commit, Tree
from dulwich.object_store import iter_tree_contents
from dulwich.protocol import pkt_line
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import HTTPGitApplication
from urllib3.response import HTTPResponse

from issuelens_github_mcp.wiki import WikiError, WikiRepository, _WikiHttpClient


class WikiFixture:
    def setUp(self) -> None:
        for guard in (patch.dict(os.environ, {"PATH": ""}),
                      patch("subprocess.Popen", side_effect=AssertionError("no subprocess")),
                      patch("os.system", side_effect=AssertionError("no shell")),
                      patch.object(StackedConfig, "default", side_effect=AssertionError("no ambient config"))):
            guard.start()
            self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory(prefix="wiki-transport-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.remote = self.directory / "remote.git"
        self.remote_repo = Repo.init_bare(str(self.remote), mkdir=True, config=StackedConfig([]), default_branch=b"docs/wiki")
        self.addCleanup(self.remote_repo.close)
        blob = Blob.from_string(b"# Home\nWelcome\n")
        tree = Tree()
        tree.add(b"Home.md", 0o100644, blob.id)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = []
        commit.author = commit.committer = b"Fixture <fixture@example.test>"
        commit.author_time = commit.commit_time = 1
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = b"Seed\n"
        for obj in (blob, tree, commit):
            self.remote_repo.object_store.add_object(obj)
        self.branch = b"refs/heads/docs/wiki"
        self.remote_repo.refs[self.branch] = commit.id
        self.remote_repo.refs.set_symbolic_ref(b"HEAD", self.branch)
        self.base = commit.id.decode("ascii")
        remote_path = str(self.directory / "remote.git")

        class LocalWiki(WikiRepository):
            def _create_client(self):
                return LocalGitClient(config=ConfigDict()), remote_path

        self.local_wiki = LocalWiki

    def tip(self, branch=None):
        return self.remote_repo.refs[branch or self.branch].decode("ascii")

    def object_id(self, sha, path):
        commit = self.remote_repo.object_store[sha.encode("ascii")]
        tree = self.remote_repo.object_store[commit.tree]
        return tree.lookup_path(self.remote_repo.object_store.__getitem__, path.encode("utf-8"))[1].decode("ascii")

    def seed_entries(self, entries, *, parents=None, branch=None):
        branch = branch or self.branch
        current = self.remote_repo.refs[branch]
        previous = self.remote_repo.object_store[current]
        inventory = {entry.path: (entry.sha, entry.mode)
                     for entry in iter_tree_contents(self.remote_repo.object_store, previous.tree)}
        for path, (mode, content) in entries.items():
            if mode == "160000":
                oid = current
            else:
                blob = Blob.from_string(content)
                self.remote_repo.object_store.add_object(blob)
                oid = blob.id
            inventory[path.encode("utf-8")] = (oid, int(mode, 8))
        tree_id = commit_tree(self.remote_repo.object_store,
                              [(path, oid, mode) for path, (oid, mode) in inventory.items()])
        commit = Commit()
        commit.tree = tree_id
        commit.parents = [current] if parents is None else parents
        commit.author = commit.committer = b"Fixture <fixture@example.test>"
        commit.author_time = commit.commit_time = previous.commit_time + 1
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = b"Fixture entries\n"
        self.remote_repo.object_store.add_object(commit)
        self.remote_repo.refs[branch] = commit.id
        return commit.id.decode("ascii")

    def write(self, wiki, pages, base=None):
        return wiki.write(pages, base or self.base, "Update team notes", author_name="IssueLens App",
                          author_email="123+issuelens[bot]@users.noreply.github.com")


class InProcessTests(WikiFixture, unittest.TestCase):
    def test_snapshot_without_any_git_executable(self) -> None:
        with self.local_wiki("example/source.git") as wiki:
            self.assertEqual(wiki.remote, "https://github.com/example/source.git.wiki.git")
            self.assertEqual(wiki.snapshot(), {"repository": "example/source.git", "branch": "docs/wiki",
                                               "sha": self.base, "initialized": True})
            self.assertEqual(wiki.page("Home.md")["content"], "# Home\nWelcome\n")

    def test_cas_rejection_after_advertisement_preserves_concurrent_commit(self):
        with self.local_wiki("example/repository") as wiki:
            generate = wiki._repo.object_store.generate_pack_data
            concurrent = []

            def race(*args, **kwargs):
                concurrent.append(self.seed_entries({"Other.md": ("100644", b"Concurrent")}))
                return generate(*args, **kwargs)

            with patch.object(wiki._repo.object_store, "generate_pack_data", side_effect=race):
                with self.assertRaisesRegex(WikiError, "conflict or outcome unknown"):
                    self.write(wiki, {"New.md": "Loser"})
            self.assertEqual(self.tip(), concurrent[0])
            self.assertEqual(wiki.snapshot()["sha"], self.base)
            with self.assertRaises(KeyError):
                self.object_id(self.tip(), "New.md")

    def test_backend_has_no_process_or_transport_autodiscovery_api(self):
        import ast
        import inspect
        import issuelens_github_mcp.wiki as module
        source = inspect.getsource(module)
        parsed = ast.parse(source)
        imports = [node.module for node in ast.walk(parsed) if isinstance(node, ast.ImportFrom)]
        imports += [name.name for node in ast.walk(parsed) if isinstance(node, ast.Import) for name in node.names]
        self.assertNotIn("subprocess", imports)
        self.assertNotIn("get_transport_and_path", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("_environment", source)

    def test_empty_directory_collisions_and_unrelated_modes_survive(self):
        self.base = self.seed_entries({"Executable.md": ("100755", b"Executable"),
                                       "link.md": ("120000", b"Home.md"),
                                       "asset.bin": ("100755", b"\x00\xff")})
        parent = self.remote_repo.object_store[self.base.encode()]
        empty = Tree()
        self.remote_repo.object_store.add_object(empty)
        tree = self.remote_repo.object_store[parent.tree]
        tree.add(b"Empty", 0o40000, empty.id)
        self.remote_repo.object_store.add_object(tree)
        parent.tree = tree.id
        self.remote_repo.object_store.add_object(parent)
        self.remote_repo.refs[self.branch] = parent.id
        self.base = parent.id.decode()
        with self.local_wiki("example/repository") as wiki:
            for batch in ({"empty/New.md": "collision"}, {"Empty": "invalid page"}):
                with self.assertRaises(WikiError):
                    self.write(wiki, batch)
            result = self.write(wiki, {"Home.md": "Update", "Executable.md": "Changed"})
            current = self.remote_repo.object_store[result["sha"].encode()]
            updated_tree = self.remote_repo.object_store[current.tree]
            self.assertEqual(updated_tree[b"Executable.md"][0], 0o100755)
            for path in (b"Empty", b"link.md", b"asset.bin"):
                self.assertEqual(updated_tree[path], tree[path])

    def test_projected_directory_count_is_checked_before_new_objects(self):
        with self.local_wiki("example/repository") as wiki:
            wiki.MAX_TREE_ENTRIES = 2
            store = wiki._repo.object_store
            before = set(store)
            with patch.object(store, "add_object", wraps=store.add_object) as add:
                with self.assertRaisesRegex(WikiError, "tree entry"):
                    self.write(wiki, {"Nested/Page.md": "New"})
            self.assertTrue(all(call.args[0].id in before for call in add.call_args_list))
            self.assertEqual(set(store), before)
            self.assertEqual(self.tip(), self.base)

    def test_cleanup_runs_even_when_client_or_repository_close_fails(self):
        for component in ("_repo", "_client"):
            wiki = self.local_wiki("example/repository").__enter__()
            parent = wiki._parent
            with patch.object(getattr(wiki, component), "close", create=True, side_effect=OSError("sensitive-close")):
                with self.assertRaises(WikiError) as raised:
                    wiki.__exit__(None, None, None)
            self.assertNotIn("sensitive-close", str(raised.exception))
            self.assertFalse(parent.exists())
            self.assertIsNone(wiki._repo)


class HttpTests(WikiFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.upload_requests = []
        self.redirect = False
        self.reject = False
        self.before_receive = None
        self.stall_body = False
        self.release_body = threading.Event()
        self.token = "synthetic-token-not-a-secret"
        application = HTTPGitApplication(DictBackend({"/wiki.git": self.remote_repo}))

        def app(environ, start_response):
            self.requests.append((environ["REQUEST_METHOD"], environ["PATH_INFO"], environ.get("HTTP_AUTHORIZATION")))
            if environ["REQUEST_METHOD"] == "POST":
                length = int(environ["CONTENT_LENGTH"])
                self.assertLess(length, 1024 * 1024)
                body = environ["wsgi.input"].read(length)
                environ["wsgi.input"] = io.BytesIO(body)
                if environ["PATH_INFO"] == "/wiki.git/git-upload-pack":
                    self.upload_requests.append(body)
            if self.redirect:
                start_response("302 Found", [("Location", "/credential-target"), ("Content-Length", "0")])
                return []
            if self.reject:
                start_response("401 Unauthorized", [("Content-Length", str(len(self.token)))])
                return [self.token.encode("ascii")]
            if self.stall_body:
                start_response("200 OK", [("Content-Type", "application/x-git-upload-pack-advertisement"),
                                          ("Content-Length", "4")])

                def body():
                    yield b"0"
                    self.release_body.wait(timeout=3)

                return body()
            if environ["PATH_INFO"] == "/wiki.git/git-receive-pack" and self.before_receive:
                self.before_receive()
            return application(environ, start_response)

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, format, *args):
                pass

        self.server = make_server("127.0.0.1", 0, app, handler_class=QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/wiki.git"
        url = self.url

        class HttpWiki(WikiRepository):
            def _create_client(self):
                return _WikiHttpClient(self, url), url

        self.http_wiki = HttpWiki

    def stop_server(self):
        self.release_body.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def test_refresh_and_write_reuse_fetched_objects_under_budget(self):
        self.base = self.seed_entries({"asset.bin": ("100644", b"a" * 2048)}, parents=[])
        repository = self.http_wiki("example/repository")
        repository.MAX_DISK_BYTES = 4096
        with repository as wiki:
            store = wiki._repo.object_store
            self.assertGreater(store.raw_bytes, wiki.MAX_DISK_BYTES // 2)
            with patch.object(wiki, "_load_pack", wraps=wiki._load_pack) as load_pack:
                with patch.object(store, "add_object", wraps=store.add_object) as add_object:
                    self.assertEqual(wiki._refresh(), self.base)
                    self.assertEqual(self.write(wiki, {"Home.md": "# Home\nWelcome\n"})["status"], "no-change")
                    add_object.assert_not_called()
                updated = self.write(wiki, {"Home.md": "Small update\n"})
                self.assertEqual(updated["status"], "updated")
                self.assertEqual(wiki._repo.refs[self.branch], updated["sha"].encode("ascii"))
                with patch.object(store, "add_object", wraps=store.add_object) as add_object:
                    self.assertEqual(self.write(wiki, {"Home.md": "Small update\n"})["status"], "no-change")
                    add_object.assert_not_called()
                load_pack.assert_not_called()
            self.assertLess(store.raw_bytes, wiki.MAX_DISK_BYTES)
            self.assertEqual(self.tip(), updated["sha"])
            self.assertEqual(sum(method == "POST" and path == "/wiki.git/git-upload-pack"
                                 for method, path, auth in self.requests), 1)

    def test_remote_update_negotiates_existing_haves_under_budget(self):
        self.base = self.seed_entries({"asset.bin": ("100644", b"a" * 2048)}, parents=[])
        repository = self.http_wiki("example/repository")
        repository.MAX_DISK_BYTES = 4096
        with repository as wiki:
            store = wiki._repo.object_store
            previous_objects = set(store)
            self.assertGreater(store.raw_bytes, wiki.MAX_DISK_BYTES // 2)
            self.assertEqual(wiki._repo.refs[self.branch], self.base.encode("ascii"))
            newer = self.seed_entries({"Home.md": ("100644", b"Remote update\n")})
            self.assertEqual(wiki.page("Home.md")["ref"], self.base)
            with patch.object(store, "add_object", wraps=store.add_object) as add_object:
                self.assertEqual(wiki._refresh(), newer)
            self.assertEqual(len(self.upload_requests), 2)
            self.assertIn(b"have " + self.base.encode("ascii") + b"\n", self.upload_requests[-1])
            self.assertIn(b"want " + newer.encode("ascii"), self.upload_requests[-1])
            self.assertEqual(len(add_object.call_args_list), 3)
            self.assertTrue(all(call.args[0].id not in previous_objects for call in add_object.call_args_list))
            self.assertLess(store.raw_bytes, wiki.MAX_DISK_BYTES)
            self.assertEqual(wiki._repo.refs[self.branch], newer.encode("ascii"))
            self.assertEqual(wiki.page("Home.md")["content"], "Remote update\n")
            self.assertEqual(wiki.page("Home.md", self.base)["content"], "# Home\nWelcome\n")

    def test_unvalidated_local_commit_is_still_wanted(self):
        with self.http_wiki("example/repository") as wiki:
            newer = self.seed_entries({"Home.md": ("100644", b"New remote page\n")})
            commit = self.remote_repo.object_store[newer.encode("ascii")]
            wiki._repo.object_store.add_object(commit)
            self.assertNotIn(commit.tree, wiki._repo.object_store)
            self.assertNotIn(commit.id, wiki._reachable)
            self.assertEqual(wiki._repo.refs[self.branch], self.base.encode("ascii"))
            with patch.object(wiki, "_load_pack", wraps=wiki._load_pack) as load_pack:
                self.assertEqual(wiki._refresh(), newer)
                load_pack.assert_called_once()
            self.assertIn(b"want " + commit.id, self.upload_requests[-1])
            self.assertIn(b"have " + self.base.encode("ascii") + b"\n", self.upload_requests[-1])
            self.assertNotIn(b"have " + commit.id, self.upload_requests[-1])
            self.assertEqual(wiki.page("Home.md")["content"], "New remote page\n")

    def test_noop_after_push_still_checks_default_branch_advertisement(self):
        with self.http_wiki("example/repository") as wiki:
            updated = self.write(wiki, {"Home.md": "Published\n"})
            alternate = b"refs/heads/other"
            self.remote_repo.refs[alternate] = updated["sha"].encode("ascii")
            self.remote_repo.refs.set_symbolic_ref(b"HEAD", alternate)
            requests_before = len(self.requests)
            with patch.object(wiki, "_load_pack") as load_pack, patch.object(wiki._client, "send_pack") as send_pack:
                with self.assertRaisesRegex(WikiError, "default branch advertisement changed"):
                    self.write(wiki, {"Home.md": "Published\n"})
                load_pack.assert_not_called()
                send_pack.assert_not_called()
            self.assertEqual([(method, path) for method, path, auth in self.requests[requests_before:]],
                             [("GET", "/wiki.git/info/refs")])
            self.assertEqual(wiki.snapshot()["sha"], updated["sha"])

    def test_noop_after_push_rejects_missing_remote_branch(self):
        with self.http_wiki("example/repository") as wiki:
            updated = self.write(wiki, {"Home.md": "Published\n"})
            del self.remote_repo.refs[self.branch]
            with patch.object(wiki, "_load_pack") as load_pack, patch.object(wiki._client, "send_pack") as send_pack:
                with self.assertRaisesRegex(WikiError, "remote branch is missing"):
                    self.write(wiki, {"Home.md": "Published\n"})
                load_pack.assert_not_called()
                send_pack.assert_not_called()
            self.assertEqual(wiki.snapshot()["sha"], updated["sha"])

    def test_smart_http_fetch_push_auth_and_followup_without_executables(self):
        with self.http_wiki("example/repository", token=self.token) as wiki:
            client = wiki._client
            self.assertEqual(wiki.page("Home.md")["ref"], self.base)
            result = self.write(wiki, {"Home.md": "Wire update\n"})
            self.assertEqual(result["status"], "updated")
            self.assertEqual(self.tip(), result["sha"])
            self.assertEqual(self.requests[-1][0], "GET")
            self.assertEqual(self.write(wiki, {"Home.md": "Wire update\n"})["status"], "no-change")
            self.assertEqual(wiki.history(), [result["sha"], self.base])
            self.assertIn("+Wire update", wiki.diff(self.base))
            self.assertEqual(list(client.config.sections()), [])
            self.assertNotIn(self.token, repr(client))
            self.assertNotIn("Authorization", repr(client.pool_manager.headers))
            for file in wiki._parent.rglob("*"):
                if file.is_file():
                    self.assertNotIn(self.token.encode(), file.read_bytes())
        expected = "Basic " + base64.b64encode(("x-access-token:" + self.token).encode()).decode()
        self.assertTrue(all(auth == expected for method, path, auth in self.requests))
        self.assertIn(("POST", "/wiki.git/git-receive-pack", expected), self.requests)
        self.assertEqual(client._responses, [])

    def test_redirect_is_never_followed_and_failure_is_sanitized(self):
        self.redirect = True
        wiki = self.http_wiki("example/repository", token=self.token)
        with self.assertRaisesRegex(WikiError, "redirects") as raised:
            wiki.__enter__()
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(self.token, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(wiki._parent)

    def test_http_error_body_never_reaches_error(self):
        self.reject = True
        with self.assertRaises(WikiError) as raised:
            with self.http_wiki("example/repository", token=self.token):
                self.fail("unauthorized response accepted")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(self.token, str(raised.exception))
        self.assertLess(len(str(raised.exception)), 200)

    def test_wire_ref_status_rejects_cas_race(self):
        concurrent = []
        self.before_receive = lambda: concurrent.append(self.seed_entries({"Other.md": ("100644", b"Other")}))
        with self.http_wiki("example/repository") as wiki:
            with self.assertRaisesRegex(WikiError, "conflict or outcome unknown"):
                self.write(wiki, {"Home.md": "Losing wire update"})
            self.assertEqual(wiki.snapshot()["sha"], self.base)
        self.assertEqual(self.tip(), concurrent[0])

    def test_wire_postpush_head_mismatch_is_unknown(self):
        with self.http_wiki("example/repository") as wiki:
            with patch.object(wiki, "_remote_tip", side_effect=OSError(self.token)):
                with self.assertRaisesRegex(WikiError, "outcome unknown") as raised:
                    self.write(wiki, {"Home.md": "Published"})
            self.assertNotIn(self.token, str(raised.exception))
            self.assertEqual(wiki.snapshot()["sha"], self.base)
        self.assertNotEqual(self.tip(), self.base)

    def test_unicode_line_separators_survive_http_wire(self):
        branch = "refs/heads/\u6587\u6863/\u2028\u2029wiki".encode("utf-8")
        self.remote_repo.refs[branch] = self.base.encode("ascii")
        self.remote_repo.refs.set_symbolic_ref(b"HEAD", branch)
        with self.http_wiki("example/repository") as wiki:
            self.assertEqual(wiki.snapshot()["branch"], branch.decode("utf-8")[11:])
            result = self.write(wiki, {"Home.md": "Unicode wire update"})
            self.assertEqual(self.tip(branch), result["sha"])
            self.assertEqual(self.tip(), self.base)

    def test_empty_smart_http_repository_is_readable_but_not_writable(self):
        del self.remote_repo.refs[self.branch]
        with self.http_wiki("example/repository") as wiki:
            self.assertFalse(wiki.snapshot()["initialized"])
            self.assertIsNone(wiki.snapshot()["sha"])
            with self.assertRaisesRegex(WikiError, "initialized"):
                self.write(wiki, {"Home.md": "New"})

    def test_real_socket_read_stall_obeys_remaining_deadline(self):
        with self.http_wiki("example/repository") as wiki:
            self.stall_body = True
            response, read = wiki._client._http_request(self.url + "/info/refs?service=git-upload-pack")
            self.assertEqual(read(1), b"0")
            wiki._deadline = time.monotonic() + 0.05
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(WikiError, "timed out|time budget"):
                    read(3)
            finally:
                self.release_body.set()
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(response.closed)

    def test_wire_pre_advertisement_race_and_post_advertisement_rejection(self):
        with self.http_wiki("example/repository") as wiki:
            send_pack = wiki._client.send_pack
            current = []

            def before_advertisement(*args, **kwargs):
                current.append(self.seed_entries({"Other.md": ("100644", b"Concurrent")}))
                return send_pack(*args, **kwargs)

            with patch.object(wiki._client, "send_pack", side_effect=before_advertisement):
                with self.assertRaisesRegex(WikiError, "conflict"):
                    self.write(wiki, {"New.md": "Never sent"})
            self.assertFalse(any(path.endswith("/git-receive-pack") for method, path, auth in self.requests))
            self.assertEqual(self.tip(), current[0])


class BudgetTests(WikiFixture, unittest.TestCase):
    @staticmethod
    def pack(objects, count=None):
        data = b"PACK" + struct.pack(">II", 2, len(objects) if count is None else count) + b"".join(objects)
        return data + hashlib.sha1(data, usedforsecurity=False).digest()

    @staticmethod
    def packed_object(kind, raw, *, declared_size=None, base=b""):
        size = len(raw) if declared_size is None else declared_size
        header = bytearray([(kind << 4) | (size & 15)])
        size >>= 4
        while size:
            header[-1] |= 128
            header.append(size & 127)
            size >>= 7
        return bytes(header) + base + zlib.compress(raw)

    def load(self, wiki, pack):
        wiki._load_pack(io.BytesIO(pack), len(pack))

    def test_native_smart_http_v2_fetch_and_cached_refresh_without_executables(self):
        commit = self.remote_repo.object_store[self.base.encode("ascii")]
        tree = self.remote_repo.object_store[commit.tree]
        blob = self.remote_repo.object_store[tree[b"Home.md"][1]]
        pack = self.pack([self.packed_object(obj.type_num, obj.as_raw_string())
                          for obj in (blob, tree, commit)])
        capabilities = b"".join(pkt_line(line) for line in (
            b"version 2\n", b"agent=protocol-fixture\n", b"ls-refs=unborn\n",
            b"fetch=shallow\n", b"server-option\n", b"object-format=sha1\n",
        )) + pkt_line(None)
        refs = (pkt_line(commit.id + b" HEAD symref-target:" + self.branch + b"\n")
                + pkt_line(commit.id + b" " + self.branch + b"\n") + pkt_line(None))
        progress = b"Enumerating objects: 3, done.\n"
        fetch = (pkt_line(b"packfile\n") + pkt_line(b"\x02" + progress)
                 + pkt_line(b"\x01" + pack[:12]) + pkt_line(b"\x01" + pack[12:])
                 + pkt_line(None))
        remote = "https://github.com/example/protocol-fixture.wiki.git"
        token = "synthetic-v2-token-not-a-secret"
        authorization = "Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
        prefixes = (("raw", b""),
                    ("github-service-banner", pkt_line(b"# service=git-upload-pack\n") + pkt_line(None)))
        for variant, prefix in prefixes:
            with self.subTest(advertisement=variant):
                advertisement = prefix + capabilities
                script = [
                    (advertisement, "application/x-git-upload-pack-advertisement"),
                    (refs, "application/x-git-upload-pack-result"),
                    (fetch, "application/x-git-upload-pack-result"),
                    (advertisement, "application/x-git-upload-pack-advertisement"),
                    (refs, "application/x-git-upload-pack-result"),
                ]
                responses = [HTTPResponse(body=io.BytesIO(body), status=200, preload_content=False,
                                          headers={"Content-Type": content_type, "Content-Length": str(len(body))})
                             for body, content_type in script]
                for response in responses:
                    self.addCleanup(response.close)
                requests = []

                def request(pool, method, url, **kwargs):
                    body = kwargs["body"]
                    payload = None if body is None else body.read()
                    requests.append((method, url, kwargs, payload))
                    return responses[len(requests) - 1]

                with patch("urllib3.PoolManager.request", autospec=True, side_effect=request) as http:
                    with WikiRepository("example/protocol-fixture", token=token) as wiki:
                        client = wiki._client
                        parent = wiki._parent
                        self.assertIsInstance(client, HttpGitClient)
                        self.assertEqual(client.protocol_version, 2)
                        self.assertEqual(wiki.remote, remote)
                        self.assertEqual(wiki._output_bytes, len(progress))
                        self.assertEqual(http.call_count, 3)
                        snapshot = wiki.snapshot()
                        page = wiki.page("Home.md")
                        self.assertEqual(snapshot, {"repository": "example/protocol-fixture",
                                                    "branch": "docs/wiki", "sha": self.base, "initialized": True})
                        self.assertEqual(page["content"], "# Home\nWelcome\n")
                        self.assertEqual(page["ref"], self.base)
                        self.assertIn(commit.id, wiki._reachable)
                        store = wiki._repo.object_store
                        self.assertEqual(set(store), {blob.id, tree.id, commit.id})
                        raw_bytes = store.raw_bytes
                        self.assertEqual(wiki._refresh(), self.base)
                        self.assertEqual(http.call_count, 5)
                        self.assertEqual(client.protocol_version, 2)
                        self.assertEqual(store.raw_bytes, raw_bytes)
                        self.assertEqual(set(store), {blob.id, tree.id, commit.id})
                        self.assertEqual(wiki._repo.refs[self.branch], commit.id)
                        self.assertEqual(wiki.snapshot(), snapshot)
                        self.assertEqual(wiki.page("Home.md"), page)
                        for result in (snapshot, page, client):
                            self.assertNotIn(token, repr(result))
                            self.assertNotIn(authorization, repr(result))
                        self.assertNotIn("Authorization", client.pool_manager.headers)
                        for path in parent.rglob("*"):
                            if path.is_file():
                                self.assertNotIn(token.encode(), path.read_bytes())
                self.assertEqual([(method, url) for method, url, kwargs, payload in requests], [
                    ("GET", remote + "/info/refs?service=git-upload-pack"),
                    ("POST", remote + "/git-upload-pack"),
                    ("POST", remote + "/git-upload-pack"),
                    ("GET", remote + "/info/refs?service=git-upload-pack"),
                    ("POST", remote + "/git-upload-pack"),
                ])
                for method, url, kwargs, payload in requests:
                    headers = kwargs["headers"]
                    self.assertEqual(headers["Git-Protocol"], "version=2")
                    self.assertEqual(headers["Authorization"], authorization)
                    self.assertFalse(kwargs["redirect"])
                    self.assertFalse(kwargs["retries"])
                    self.assertFalse(kwargs["preload_content"])
                    self.assertFalse(kwargs["decode_content"])
                    if method == "POST":
                        self.assertEqual(headers["Content-Length"], str(len(payload)))
                        self.assertEqual(headers["Content-Type"], "application/x-git-upload-pack-request")
                        self.assertIn(pkt_line(b"object-format=sha1") + b"0001", payload)
                        self.assertTrue(payload.endswith(pkt_line(None)))
                        self.assertNotIn(token.encode(), payload)
                        self.assertTrue(kwargs["body"].closed)
                    else:
                        self.assertIsNone(payload)
                for index in (1, 4):
                    payload = requests[index][3]
                    self.assertTrue(payload.startswith(pkt_line(b"command=ls-refs\n")))
                    self.assertIn(pkt_line(b"symrefs"), payload)
                    self.assertIn(pkt_line(b"unborn"), payload)
                payload = requests[2][3]
                self.assertTrue(payload.startswith(pkt_line(b"command=fetch\n")))
                self.assertIn(pkt_line(b"want " + commit.id + b"\n"), payload)
                self.assertTrue(payload.endswith(pkt_line(b"done\n") + pkt_line(None)))
                for response, (body, content_type) in zip(responses, script):
                    self.assertEqual(response.tell(), len(body))
                    self.assertTrue(response.closed)
                self.assertEqual(client._responses, [])
                self.assertFalse(parent.exists())
                self.assertIsNone(wiki._parent)
                self.assertIsNone(wiki._repo)

    def test_incomplete_fetched_history_never_becomes_a_have(self):
        with self.local_wiki("example/repository") as wiki:
            for missing in ("tree", "blob"):
                with self.subTest(missing=missing):
                    tree = Tree()
                    tree.add(b"Home.md", 0o100644, b"0" * 40)
                    commit = wiki._repo.object_store[self.base.encode("ascii")]
                    commit.tree = tree.id
                    commit.parents = [self.base.encode("ascii")]
                    commit.message = missing.encode("ascii")
                    objects = [self.packed_object(Commit.type_num, commit.as_raw_string())]
                    if missing == "blob":
                        objects.append(self.packed_object(Tree.type_num, tree.as_raw_string()))
                    pack = self.pack(objects)

                    def incomplete_fetch(client, path, wants, walker, sink, **kwargs):
                        refs = {b"HEAD": commit.id, self.branch: commit.id}
                        self.assertEqual(wants(refs), [commit.id])
                        sink(pack)
                        return FetchPackResult(refs, {b"HEAD": self.branch}, None)

                    with patch.object(LocalGitClient, "fetch_pack", new=incomplete_fetch):
                        with self.assertRaises(WikiError):
                            self.write(wiki, {"Home.md": "# Home\nWelcome\n"})
                    self.assertNotIn(commit.id, wiki._reachable)
                    self.assertEqual(wiki._repo.refs[self.branch], self.base.encode("ascii"))
                    self.assertEqual(wiki.snapshot()["sha"], self.base)

    def test_pack_size_rejected_before_decompression(self):
        wiki = self.local_wiki("example/repository")
        wiki.MAX_DISK_BYTES = 8

        def oversized(client, path, wants, walker, sink, **kwargs):
            sink(b"PACK" + b"x" * 5)

        with (patch.object(LocalGitClient, "fetch_pack", new=oversized),
              patch("issuelens_github_mcp.wiki.zlib.decompressobj",
                    side_effect=AssertionError("should not decompress"))):
            with self.assertRaisesRegex(WikiError, "pack storage"):
                wiki.__enter__()
        self.assertIsNone(wiki._parent)

    def test_zlib_size_lie_is_rejected_without_unbounded_expansion(self):
        with self.local_wiki("example/repository") as wiki:
            malicious = self.pack([self.packed_object(3, b"a" * 1_000_000, declared_size=1)])
            with self.assertRaisesRegex(WikiError, "expanded object byte"):
                self.load(wiki, malicious)

    def test_declared_size_count_total_object_and_checksum_budgets(self):
        with self.local_wiki("example/repository") as wiki:
            packs = [(self.pack([self.packed_object(3, b"x", declared_size=wiki.MAX_OBJECT_BYTES + 1)]), "object byte"),
                     (self.pack([], count=wiki.MAX_DISK_ENTRIES + 1), "object count"),
                     (self.pack([])[:-1] + b"!", "checksum")]
            for pack, error in packs:
                with self.subTest(error=error), self.assertRaisesRegex(WikiError, error):
                    self.load(wiki, pack)
            wiki.MAX_DISK_BYTES = wiki._repo.object_store.raw_bytes + 100
            with self.assertRaisesRegex(WikiError, "storage budget"):
                self.load(wiki, self.pack([self.packed_object(3, b"x" * 200)]))

    def test_metadata_byte_limit_is_checked_before_object_parser(self):
        with self.local_wiki("example/repository") as wiki:
            pack = self.pack([self.packed_object(2, b"x", declared_size=wiki.MAX_READ_BYTES + 1)])
            with patch("issuelens_github_mcp.wiki.ShaFile.from_raw_string", side_effect=AssertionError("parse called")):
                with self.assertRaisesRegex(WikiError, "structural object"):
                    self.load(wiki, pack)

    def test_store_count_bytes_and_parent_fanout_are_bounded(self):
        with self.local_wiki("example/repository") as wiki:
            store = wiki._repo.object_store
            wiki.MAX_DISK_ENTRIES = len(list(store))
            with self.assertRaisesRegex(WikiError, "object storage"):
                store.add_object(Blob.from_string(b"different"))
            wiki.MAX_DISK_ENTRIES = 20_000
            wiki.MAX_DISK_BYTES = store.raw_bytes
            with self.assertRaisesRegex(WikiError, "object storage"):
                store.add_object(Blob.from_string(b"different"))
            wiki.MAX_DISK_BYTES = 64 * 1024 * 1024
            commit = store[self.base.encode()]
            commit.parents = [self.base.encode()] * 65
            with self.assertRaisesRegex(WikiError, "parent budget"):
                store.add_object(commit)

    def test_diff_total_read_and_comparison_work_are_bounded(self):
        self.base = self.seed_entries({"Home.md": ("100644", b"a\n" * 2001), "Other.md": ("100644", b"Original")})
        newer = self.seed_entries({"Home.md": ("100644", b"b\n" * 2001)})
        with self.local_wiki("example/repository") as wiki:
            with self.assertRaisesRegex(WikiError, "comparison budget"):
                wiki.diff(self.base, newer)
            wiki.MAX_READ_BYTES = 5000
            with self.assertRaisesRegex(WikiError, "diff read byte"):
                wiki.diff(self.base, newer)

    def test_diff_preserves_unicode_line_separator_as_content(self):
        newer = self.seed_entries({"Home.md": ("100644", "Hello\u2028world\u2029end\n".encode())})
        with self.local_wiki("example/repository") as wiki:
            self.assertIn("+Hello\u2028world\u2029end\n", wiki.diff(self.base, newer))

    def test_real_ref_and_offset_deltas_and_delta_limits(self):
        with self.local_wiki("example/repository") as wiki:
            source = Blob.from_string(b"abc")
            base = self.packed_object(3, b"abc")
            delta = b"\x03\x04\x90\x03\x01!"
            reference = self.packed_object(7, delta, base=bytes.fromhex(source.id.decode()))
            offset = self.packed_object(6, delta, base=bytes([len(base)]))
            for dependent in (reference, offset):
                self.load(wiki, self.pack([base, dependent]))
                self.assertEqual(wiki._repo.object_store[Blob.from_string(b"abc!").id].data, b"abc!")
            for delta in (b"\x03\x01\x90\x03", b"\x03\x04\x90\x03", b"\x03\x01\x00", b"\xff" * 10):
                with self.assertRaises(WikiError):
                    self.load(wiki, self.pack([base, self.packed_object(7, delta, base=bytes.fromhex(source.id.decode()))]))
            wiki.MAX_DELTA_DEPTH = 0
            with self.assertRaisesRegex(WikiError, "delta depth"):
                self.load(wiki, self.pack([base, reference]))

    def test_unsupported_hash_missing_symref_and_detached_commit_fail(self):
        with self.local_wiki("example/repository") as wiki:
            for ref in ("f" * 64, "0" * 40):
                with self.assertRaisesRegex(WikiError, "commit unavailable"):
                    wiki.pages(ref)
            orphan = self.seed_entries({}, parents=[])
            with self.assertRaises(WikiError):
                wiki.pages(orphan)
        with patch.object(LocalGitClient, "fetch_pack", return_value=FetchPackResult({b"HEAD": self.base.encode()}, {}, None)):
            with self.assertRaisesRegex(WikiError, "default branch"):
                with self.local_wiki("example/repository"):
                    self.fail("missing symref accepted")

    def test_http_read_timeout_budget_and_no_unbounded_reads(self):
        with self.local_wiki("example/repository") as wiki:
            client = _WikiHttpClient(wiki, wiki.remote)
            self.addCleanup(client.close)
            response = Mock(spec=HTTPResponse, status=200, headers={"Content-Type": "application/x-git-upload-pack-advertisement"})
            response.connection = SimpleNamespace(sock=Mock(spec=socket.socket))
            response.read1.side_effect = [b"ab", b"cd"]
            with patch.object(client.pool_manager, "request", return_value=response) as request:
                returned, read = client._http_request(wiki.remote + "/info/refs?service=git-upload-pack")
                self.assertEqual(read(4), b"abcd")
                self.assertFalse(request.call_args.kwargs["redirect"])
                self.assertFalse(request.call_args.kwargs["retries"])
                self.assertFalse(request.call_args.kwargs["preload_content"])
                self.assertLessEqual(request.call_args.kwargs["timeout"].read_timeout, 5)
                self.assertTrue(response.connection.sock.settimeout.called)
                with self.assertRaisesRegex(WikiError, "read budget"):
                    read(-1)
                response.read1.side_effect = socket.timeout("synthetic-sensitive-detail")
                with self.assertRaisesRegex(WikiError, "timed out") as raised:
                    read(1)
                self.assertNotIn("synthetic-sensitive-detail", str(raised.exception))
                response.read1.side_effect = [b"xx"]
                wiki.MAX_DISK_BYTES = wiki._wire_bytes + 1
                with self.assertRaisesRegex(WikiError, "transport byte"):
                    read(2)
                wiki._deadline = time.monotonic() - 1
                with self.assertRaisesRegex(WikiError, "time budget"):
                    read(1)

    def test_http_origin_and_config_are_explicit(self):
        with self.local_wiki("example/repository", token="synthetic-token") as wiki:
            with patch.dict(os.environ, {"HTTPS_PROXY": "http://bad.invalid", "GIT_CONFIG_GLOBAL": "bad-file"}):
                client = _WikiHttpClient(wiki, wiki.remote)
            self.addCleanup(client.close)
            self.assertEqual(list(client.config.sections()), [])
            self.assertNotIn("proxy", client.pool_manager.connection_pool_kw)
            with patch.object(client.pool_manager, "request") as request:
                for url in ("http://github.com/example/repository.wiki.git/git-upload-pack", "https://other.invalid/git-upload-pack",
                            wiki.remote + "/../other/git-upload-pack", "file:///fixture", "ssh://fixture"):
                    with self.assertRaisesRegex(WikiError, "URL"):
                        client._http_request(url)
                request.assert_not_called()

    def test_http_metadata_and_outbound_bodies_share_bounded_budgets(self):
        with self.local_wiki("example/repository") as wiki:
            client = _WikiHttpClient(wiki, wiki.remote)
            self.addCleanup(client.close)
            response = Mock(spec=HTTPResponse, status=200, headers={"Content-Type": "application/x-git-upload-pack-advertisement"})
            response.connection = SimpleNamespace(sock=Mock(spec=socket.socket))
            response.read1.side_effect = [b"12345"]
            wiki.MAX_OUTPUT_BYTES = 4
            with patch.object(client.pool_manager, "request", return_value=response) as request:
                returned, read = client._http_request(wiki.remote + "/info/refs?service=git-upload-pack")
                with self.assertRaisesRegex(WikiError, "response byte"):
                    read(5)
                request.reset_mock()
                wiki.MAX_DISK_BYTES = wiki._wire_bytes + 4
                with self.assertRaisesRegex(WikiError, "transport byte"):
                    client._http_request(wiki.remote + "/git-upload-pack", data=b"12345")
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
