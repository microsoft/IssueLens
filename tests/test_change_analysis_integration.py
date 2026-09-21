"""Large-change evidence through real MCP framing and bounded model callbacks."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import pathlib
import sys
import unittest

import httpx
from dulwich.objects import Tree
from mcp import Client

from change_analysis import MODEL_CONTEXT_RESERVE_BYTES, AnalysisLimits, analyze_change
from change_analysis_tool import AnalysisRuntimeError, source_reader
from github_app_mcp.src.issuelens_github_mcp.auth import GitHubAppError, InstallationCredential
from github_app_mcp.src.issuelens_github_mcp.github import GitHubClient
from github_app_mcp.src.issuelens_github_mcp.server import create_server
from telemetry import RunTelemetry, Settings

if __package__:
    from .test_telemetry import RecordingBackend
else:
    from test_telemetry import RecordingBackend


REPOSITORY = "owner/repo"
BASE = "a" * 40
HEAD = "b" * 40
RAW_MARKER = "RAW-DIFF-CANARY"


class SourceFixture:
    def __init__(self, *, file_count=23, new_lines=1000):
        self.requests = []
        self.permissions = []
        self.blobs = {}
        self.trees = {}
        before, after = [], []
        for index in range(file_count):
            path = f"module{index:02}.py"
            old = (f"old_{index}\n" * 250).encode()
            new = (f"{RAW_MARKER}_{index}\n" * new_lines).encode()
            for entries, content in ((before, old), (after, new)):
                entries.append(self.file(path, content))
        self.commits = {
            BASE: {"sha": BASE, "tree": {"sha": self.tree(before)}, "parents": []},
            HEAD: {"sha": HEAD, "tree": {"sha": self.tree(after)}, "parents": [{"sha": BASE}]},
        }
        self.client = GitHubClient(self, transport=httpx.MockTransport(self.handle))

    def file(self, path, content):
        sha = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        self.blobs[sha] = content
        return {
            "path": path, "type": "blob", "mode": "100644",
            "sha": sha, "size": len(content),
        }

    def directory(self, path, entries):
        return {"path": path, "type": "tree", "mode": "040000", "sha": self.tree(entries)}

    def tree(self, entries):
        tree = Tree()
        for entry in entries:
            tree.add(entry["path"].encode(), int(entry["mode"], 8), entry["sha"].encode())
        sha = tree.id.decode()
        self.trees[sha] = {"sha": sha, "tree": entries, "truncated": False}
        return sha

    async def get_token(self, repository, permissions):
        self.permissions.append((repository, permissions))
        return InstallationCredential(
            installation_id=1, repository=repository,
            permissions=tuple(sorted(permissions.items())),
            token="fixture-only-token", expires_at=float("inf"),
        )

    def handle(self, request):
        self.requests.append(request)
        prefix = f"/repos/{REPOSITORY}/"
        if request.method != "GET" or request.url.host != "api.github.com":
            raise AssertionError("Unexpected network operation")
        if not request.url.path.startswith(prefix):
            raise AssertionError("Repository scope changed")
        route = request.url.path[len(prefix):]
        if route == f"commits/{HEAD}":
            return httpx.Response(200, json={
                "sha": HEAD, "files": [{"filename": "huge.py", "patch": "x" * 405_679}],
            })
        if route.startswith("git/commits/"):
            return httpx.Response(200, json=self.commits[route.removeprefix("git/commits/")])
        if route.startswith("git/trees/"):
            return httpx.Response(200, json=self.trees[route.removeprefix("git/trees/")])
        if route.startswith("git/blobs/"):
            sha = route.removeprefix("git/blobs/")
            content = self.blobs[sha]
            if "raw" in request.headers.get("accept", ""):
                return httpx.Response(200, content=content)
            return httpx.Response(200, json={
                "sha": sha, "size": len(content), "encoding": "base64",
                "content": base64.b64encode(content).decode(),
            })
        raise AssertionError(f"Unexpected fixture route: {route}")


def evidence_ids(value):
    result = set()
    if isinstance(value, dict):
        if isinstance(value.get("chunk_id"), str):
            result.add(value["chunk_id"])
        for key, child in value.items():
            if key == "evidence" and isinstance(child, list):
                result.update(item for item in child if isinstance(item, str))
            result.update(evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            result.update(evidence_ids(child))
    return result


class ChangeAnalysisIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logger = logging.getLogger("httpx")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)

    async def analyze_source(self, source):
        prompts = []
        result_sizes = []
        limits = AnalysisLimits()
        server = create_server(source.client)

        async def model(phase, prompt):
            self.assertLessEqual(
                len(prompt.encode()) + MODEL_CONTEXT_RESERVE_BYTES, limits.max_prompt_bytes,
            )
            payload = json.loads(prompt)
            ids = sorted(evidence_ids(payload.get("chunks", payload.get("reports", []))))
            prompts.append((phase, prompt))
            if phase == "reduce":
                self.assertNotIn(RAW_MARKER, prompt)
            report = {
                "summary": "Changed implementation.",
                "findings": [{"text": "Implementation changed.", "evidence": ids[:1]}] if ids else [],
            }
            if phase != "reduce":
                report["reviewed_chunks"] = ids
                report["needs_context"] = []
            return json.dumps(report)

        async with Client(server, mode="legacy") as mcp:
            async def read(name, arguments):
                response = await mcp.call_tool(name, arguments=arguments)
                self.assertFalse(response.is_error, response.content)
                self.assertEqual(len(response.content), 1)
                text = response.content[0].text
                result_sizes.append(len(text.encode()))
                self.assertLessEqual(result_sizes[-1], arguments.get("max_bytes", 32768))
                return json.loads(text)

            result = await analyze_change(
                read, model, repository=REPOSITORY, commit_sha=HEAD, limits=limits,
            )
        return result, prompts

    async def test_oversized_legacy_commit_has_a_complete_bounded_analysis_path(self):
        source = SourceFixture(file_count=2, new_lines=65000)
        self.assertGreater(sum(map(len, source.blobs.values())), 2 * 1024 * 1024)
        self.assertGreater(max(map(len, source.blobs.values())), 1024 * 1024)
        with self.assertRaisesRegex(GitHubAppError, "too large"):
            await source.client.get_commit(REPOSITORY, HEAD)
        source.requests.clear()
        result, prompts = await self.analyze_source(source)
        self.assertEqual(result["status"], "complete", result)
        self.assertTrue(result["coverage"]["inventory_complete"])
        self.assertEqual(result["coverage"]["files_reviewed"], 2)
        self.assertEqual(result["coverage"]["chunks_reviewed"], result["coverage"]["chunks_discovered"])
        self.assertGreater(sum(phase == "map" for phase, _ in prompts), 1)
        self.assertTrue(any(phase == "reduce" for phase, _ in prompts))
        self.assertNotIn(RAW_MARKER, json.dumps(result))
        self.assertTrue(all(permission == {"contents": "read"} for _, permission in source.permissions))
        self.assertTrue(all(repo == REPOSITORY for repo, _ in source.permissions))
        self.assertFalse(any(
            request.url.path == f"/repos/{REPOSITORY}/commits/{HEAD}"
            for request in source.requests
        ))

    async def test_directory_file_transitions_have_complete_analysis_coverage(self):
        source = SourceFixture(file_count=0)
        before = [
            source.file("becomes-dir", b"old root\n" * 1000),
            source.directory("becomes-file", [
                source.directory("nested", [
                    source.file("child.py", b"old nested\n" * 1000),
                ]),
            ]),
        ]
        after = [
            source.directory("becomes-dir", [
                source.directory("nested", [
                    source.file("child.py", b"new nested\n" * 1000),
                ]),
            ]),
            source.file("becomes-file", b"new root\n" * 1000),
        ]
        source.commits[BASE]["tree"]["sha"] = source.tree(before)
        source.commits[HEAD]["tree"]["sha"] = source.tree(after)
        result, prompts = await self.analyze_source(source)
        self.assertEqual(result["status"], "complete", result)
        self.assertTrue(result["coverage"]["inventory_complete"])
        self.assertEqual(result["coverage"]["files_discovered"], 4)
        self.assertEqual(result["coverage"]["files_reviewed"], 4)
        self.assertEqual(result["coverage"]["unresolved_count"], 0)
        self.assertEqual(
            result["coverage"]["chunks_reviewed"], result["coverage"]["chunks_discovered"],
        )
        paths = {
            chunk["path"] for phase, prompt in prompts if phase == "map"
            for chunk in json.loads(prompt)["chunks"]
        }
        self.assertEqual(paths, {
            "becomes-dir", "becomes-dir/nested/child.py",
            "becomes-file", "becomes-file/nested/child.py",
        })

    async def test_stdio_bridge_reads_real_mcp_envelopes(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        server = {
            "command": sys.executable,
            "args": ["-c", (
                "import logging\n"
                "from test_change_analysis_integration import SourceFixture\n"
                "from github_app_mcp.src.issuelens_github_mcp.server import create_server\n"
                "logging.getLogger('httpx').setLevel(logging.WARNING)\n"
                "create_server(SourceFixture().client).run(transport='stdio')\n"
            )],
            "working_directory": str(root),
            "env": {
                "PYTHONPATH": os.pathsep.join((str(root / "tests"), str(root))),
                "GITHUB_MCP_ENABLE_WRITES": "true",
            },
        }
        backend = RecordingBackend()
        self.addCleanup(backend.provider.shutdown)
        run = RunTelemetry(backend, Settings(), "invocations")
        self.addCleanup(run.finish, "completed")
        async with asyncio.timeout(30), source_reader(server, run) as read:
            manifest = await read("list_change_files", {
                "repository": REPOSITORY, "commit_sha": HEAD, "per_page": 2,
            })
            self.assertEqual(len(manifest["files"]), 2)
            self.assertFalse(manifest["complete"])
            self.assertTrue(manifest["next_cursor"])
            self.assertEqual(manifest["snapshot"]["head_sha"], HEAD)
            path = manifest["files"][0]["path"]
            chunk = await read("read_diff_chunk", {
                "repository": REPOSITORY, "base_sha": BASE, "head_sha": HEAD,
                "path": path, "max_bytes": 4096,
            })
            self.assertEqual(chunk["snapshot_id"], manifest["snapshot"]["snapshot_id"])
            self.assertEqual(chunk["status"], "text")
            context = await read("read_file_range", {
                "repository": REPOSITORY, "sha": HEAD, "path": path,
                "start_line": 1, "end_line": 3, "max_bytes": 4096,
            })
            self.assertIn(RAW_MARKER, context["content"])
            with self.assertRaisesRegex(AnalysisRuntimeError, "unsupported_read"):
                await read("write_wiki_pages", {"repository": REPOSITORY})
        self.assertEqual(run.analysis_counts["analysis_reads"], 3)
        self.assertNotIn(RAW_MARKER, repr(backend.events))


if __name__ == "__main__":
    unittest.main()
