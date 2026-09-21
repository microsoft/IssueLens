import asyncio
import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace

from change_analysis import (
    AnalysisLimits,
    MAX_CHUNK_BYTES,
    MAX_FOCUS_BYTES,
    MAX_PROMPT_BYTES,
    MAX_REPORT_BYTES,
    MAX_REDUCTION_FAN_IN,
    MAX_RESULT_BYTES,
    MODEL_CONTEXT_RESERVE_BYTES,
    analyze_change,
)


REPOSITORY = "owner/project"
BASE = "a" * 40
HEAD = "b" * 40
OLD_BLOB = "c" * 40
NEW_BLOB = "d" * 40
SECRET = "private-exception-payload-do-not-disclose"
RAW_MARKER = "RAW_DIFF_NOT_A_REDUCER_INPUT"


class Activity:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.task_peak = 0
        self.task_baseline = len(asyncio.all_tasks())

    def enter(self):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.task_peak = max(self.task_peak, len(asyncio.all_tasks()) - self.task_baseline)

    def leave(self):
        self.active -= 1


class FakeSource:
    def __init__(
        self, data=None, *, page_size=50, chunk_size=MAX_CHUNK_BYTES,
        mutate=None, activity=None, final_json_limit=False,
    ):
        self.data = {"file.py": "+new source\n"} if data is None else data
        self.page_size = page_size
        self.chunk_size = chunk_size
        self.mutate = mutate
        self.final_json_limit = final_json_limit
        self.activity = activity or Activity()
        self.calls = []
        self.context_data = "Owning interface and implementation evidence."

    @staticmethod
    def snapshot(arguments):
        snapshot = {
            "snapshot_id": "pinned-snapshot", "base_sha": BASE, "head_sha": HEAD,
            "mode": "pull" if "pull_number" in arguments else "commit" if "commit_sha" in arguments else "compare",
        }
        if "pull_number" in arguments:
            snapshot["pull_number"] = arguments["pull_number"]
        return snapshot

    async def __call__(self, name, arguments):
        self.activity.enter()
        try:
            self.calls.append((name, copy.deepcopy(arguments)))
            await asyncio.sleep(0)
            if name == "list_change_files":
                offset = int(arguments.get("cursor", "0"))
                paths = list(self.data)
                end = min(len(paths), offset + min(arguments["per_page"], self.page_size))
                response = {
                    "repository": REPOSITORY, "snapshot": self.snapshot(arguments),
                    "files": [{
                        "path": path, "status": "modified",
                        "old_blob_sha": OLD_BLOB, "new_blob_sha": NEW_BLOB,
                        "old_mode": "100644", "new_mode": "100644",
                    } for path in paths[offset:end]],
                    "next_cursor": None if end == len(paths) else str(end),
                    "complete": end == len(paths),
                }
            elif name in ("read_diff_chunk", "read_file_range"):
                path = arguments["path"]
                data = self.data[path] if name == "read_diff_chunk" else self.context_data
                offset = int(arguments.get("cursor", "0"))
                byte_limit = min(self.chunk_size, arguments["max_bytes"])
                content = data[offset:].encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")
                end = offset + len(content)
                response = {
                    "repository": REPOSITORY, "path": path,
                    "chunk_id": hashlib.sha256(path.encode()).hexdigest()[:12] + ":" + str(offset),
                    "content": content, "status": "text",
                    "next_cursor": None if end == len(data) else str(end),
                    "complete": end == len(data),
                }
                if name == "read_diff_chunk":
                    response.update({
                        "snapshot_id": "pinned-snapshot",
                        "old_blob_sha": OLD_BLOB, "new_blob_sha": NEW_BLOB,
                        "old_start": 1, "old_end": 1, "new_start": 1, "new_end": 1,
                        "representation": "unified",
                    })
                else:
                    response.update({
                        "sha": arguments["sha"],
                        "start_line": arguments["start_line"],
                        "end_line": arguments["start_line"],
                    })
                if self.final_json_limit:
                    def framed(length):
                        position = offset + length
                        return {
                            **response, "content": content[:length],
                            "next_cursor": None if position == len(data) else str(position),
                            "complete": position == len(data),
                        }

                    low, high = 0, len(content)
                    while low < high:
                        middle = (low + high + 1) // 2
                        if len(json.dumps(framed(middle)).encode()) <= arguments["max_bytes"]:
                            low = middle
                        else:
                            high = middle - 1
                    assert low
                    response = framed(low)
            else:
                raise AssertionError("Unexpected tool")
            if self.mutate:
                mutated = self.mutate(name, arguments, response)
                if mutated is not None:
                    response = mutated
            return response
        finally:
            self.activity.leave()


class FakeModel:
    def __init__(self, *, mutate=None, activity=None, capture=False, reorder=False):
        self.mutate = mutate
        self.activity = activity or Activity()
        self.capture = capture
        self.reorder = reorder
        self.calls = []
        self.output_sizes = []
        self.delivered = {}

    async def __call__(self, phase, prompt):
        self.activity.enter()
        try:
            document = json.loads(prompt)
            call_number = len(self.calls)
            size = len(prompt.encode("utf-8")) + MODEL_CONTEXT_RESERVE_BYTES
            self.calls.append({
                "phase": phase, "size": size,
                "raw_in_reducer": phase == "reduce" and RAW_MARKER in prompt,
                "prompt": document if self.capture else None,
            })
            assert size <= MAX_PROMPT_BYTES
            assert document["phase"] == phase
            await asyncio.sleep(0.0001 if self.reorder and phase == "map" else 0)
            if phase == "reduce":
                assert "chunks" not in document
                assert 2 <= len(document["reports"]) <= MAX_REDUCTION_FAN_IN
                ids = list(dict.fromkeys(
                    reference for report in document["reports"]
                    for finding in report["findings"] for reference in finding["evidence"]
                ))[:3]
                report = {
                    "summary": "Combined bounded evidence.",
                    "findings": [{"text": "Supported conclusion.", "evidence": ids}] if ids else [],
                }
            else:
                assert len(document["chunks"]) == 1
                chunk = document["chunks"][0]
                if self.capture:
                    self.delivered.setdefault(chunk["chunk_id"], chunk["content"])
                ids = [chunk["chunk_id"]]
                report = {
                    "reviewed_chunks": ids, "summary": "Reviewed bounded source evidence.",
                    "findings": [{"text": "Supported observation.", "evidence": ids}],
                    "needs_context": [],
                }
            if self.mutate:
                report = self.mutate(phase, document, report, call_number)
            raw = json.dumps(report) if isinstance(report, dict) else report
            if isinstance(raw, str):
                self.output_sizes.append(len(raw.encode("utf-8")))
            return raw
        finally:
            self.activity.leave()


def context_request(**changes):
    return {
        "path": "implementation.py", "side": "base",
        "start_line": 1, "end_line": 10, "reason": "Check the owning implementation.",
        **changes,
    }


class ChangeAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def run_analysis(self, source=None, model=None, **kwargs):
        source = source or FakeSource()
        model = model or FakeModel()
        selector = kwargs.pop("selector", {"pull_number": 17})
        result = await analyze_change(
            source, model, repository=REPOSITORY, **selector, **kwargs,
        )
        limit = kwargs.get("limits", AnalysisLimits()).max_result_bytes
        self.assertLessEqual(len(json.dumps(result).encode("utf-8")), limit)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn(result["status"], ("complete", "partial", "blocked"))
        return result, source, model

    def test_limits_are_frozen_and_finite(self):
        with self.assertRaises(FrozenInstanceError):
            AnalysisLimits().concurrency = 4
        for change in (
            {"max_seconds": float("inf")}, {"max_seconds": float("nan")},
            {"max_seconds": 0}, {"max_seconds": True}, {"concurrency": 0},
            {"concurrency": 1000}, {"max_files": -1}, {"max_model_calls": True},
            {"max_prompt_bytes": MAX_PROMPT_BYTES + 1},
            {"max_result_bytes": MAX_RESULT_BYTES + 1},
            {"max_result_bytes": 1},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(AnalysisLimits(), **change)

    async def test_rejects_invalid_selectors_and_unbounded_guidance_without_io(self):
        for selector, focus in (
            ({}, ""), ({"pull_number": 1, "commit_sha": HEAD}, ""),
            ({"base_sha": BASE}, ""), ({"pull_number": True}, ""),
            ({"commit_sha": "main"}, ""), ({"pull_number": 1}, "x" * MAX_FOCUS_BYTES),
            ({"pull_number": 1}, "\ud800"),
        ):
            with self.subTest(selector=selector, focus_length=len(focus)):
                result, source, model = await self.run_analysis(selector=selector, focus=focus)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["coverage"]["unresolved_by_code"], {"invalid_input": 1})
                self.assertEqual(source.calls, [])
                self.assertEqual(model.calls, [])

    async def test_empty_inventory_is_complete_without_model_calls(self):
        result, source, model = await self.run_analysis(FakeSource({}))
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["coverage"]["inventory_complete"])
        self.assertEqual(result["coverage"]["files_discovered"], 0)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(model.calls, [])

    async def test_root_commit_uses_explicit_absent_base_and_pinned_head(self):
        def mutate(name, arguments, response):
            if name == "list_change_files":
                response["snapshot"]["base_sha"] = None
                response["files"][0].update(old_blob_sha=None, old_mode=None, status="added")
            elif name == "read_diff_chunk":
                response.update(old_blob_sha=None, old_start=0, old_end=0)
            return response

        result, source, _ = await self.run_analysis(
            FakeSource(mutate=mutate), selector={"commit_sha": HEAD},
        )
        self.assertEqual(result["status"], "complete")
        self.assertIsNone(result["snapshot"]["base_sha"])
        reads = [arguments for name, arguments in source.calls if name == "read_diff_chunk"]
        self.assertEqual(len(reads), 1)
        self.assertIsNone(reads[0]["base_sha"])
        self.assertEqual(reads[0]["head_sha"], HEAD)
        self.assertIsNone(result["findings"][0]["citations"][0]["base_sha"])

    async def test_root_commit_base_context_is_unresolved_without_a_read(self):
        def source_mutation(name, arguments, response):
            if name == "list_change_files":
                response["snapshot"]["base_sha"] = None
            return response

        def model_mutation(phase, document, report, call_number):
            if phase == "map":
                report["needs_context"] = [context_request(side="base")]
            return report

        result, source, _ = await self.run_analysis(
            FakeSource(mutate=source_mutation), FakeModel(mutate=model_mutation),
            selector={"commit_sha": HEAD},
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["context_requests_unresolved"], 1)
        self.assertEqual(result["coverage"]["unresolved_by_code"], {"base_context_unavailable": 1})
        self.assertFalse(any(name == "read_file_range" for name, _ in source.calls))

    async def test_absent_base_is_rejected_for_non_root_selectors(self):
        def mutate(name, arguments, response):
            if name == "list_change_files":
                response["snapshot"]["base_sha"] = None
            return response

        for selector in ({"pull_number": 17}, {"base_sha": BASE, "head_sha": HEAD}):
            with self.subTest(selector=selector):
                result, _, model = await self.run_analysis(FakeSource(mutate=mutate), selector=selector)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["coverage"]["unresolved_by_code"], {"snapshot_mismatch": 1})
                self.assertEqual(model.calls, [])

    async def test_pins_inventory_and_preserves_original_selectors_on_every_page(self):
        data = {f"file-{index}.py": "+source" for index in range(5)}
        for selector in ({"pull_number": 17}, {"commit_sha": HEAD}, {"base_sha": BASE, "head_sha": HEAD}):
            with self.subTest(selector=selector):
                result, source, model = await self.run_analysis(FakeSource(data, page_size=2), selector=selector)
                self.assertEqual(result["status"], "complete")
                coverage = result["coverage"]
                self.assertEqual(coverage["files_discovered"], 5)
                self.assertEqual(coverage["files_reviewed"], 5)
                self.assertEqual(coverage["chunks_reviewed"], 5)
                self.assertEqual(coverage["reports_in_summary"], 5)
                self.assertEqual(result["counters"]["model_calls"], 6)
                self.assertEqual(len(model.calls), 6)
                inventories = [arguments for name, arguments in source.calls if name == "list_change_files"]
                self.assertEqual(len(inventories), 3)
                for arguments in inventories:
                    self.assertEqual(
                        {key: value for key, value in arguments.items() if key not in ("repository", "cursor", "per_page")},
                        selector,
                    )
                for name, arguments in source.calls:
                    if name == "read_diff_chunk":
                        self.assertEqual((arguments["base_sha"], arguments["head_sha"]), (BASE, HEAD))
                        self.assertLessEqual(arguments["max_bytes"], MAX_CHUNK_BYTES)
                for finding in result["findings"]:
                    for citation in finding["citations"]:
                        self.assertIn(citation["path"], data)
                        self.assertEqual(citation["snapshot_id"], "pinned-snapshot")
                        self.assertEqual(citation["repository"], REPOSITORY)
                        self.assertEqual((citation["base_sha"], citation["head_sha"]), (BASE, HEAD))

    async def test_more_than_two_megabytes_aggregate_uses_many_bounded_contexts(self):
        unit = RAW_MARKER + "\n+" + "x" * 101
        file_text = (unit * (131_072 // len(unit) + 1))[:131_072]
        data = {f"source-{index}.py": file_text for index in range(17)}
        self.assertGreater(sum(len(text.encode()) for text in data.values()), 2 * 1_024 * 1_024)
        result, source, model = await self.run_analysis(FakeSource(data, final_json_limit=True))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["files_reviewed"], 17)
        self.assertGreater(result["coverage"]["chunks_reviewed"], 17 * 32)
        self.assertEqual(
            result["coverage"]["chunks_reviewed"],
            sum(name == "read_diff_chunk" for name, _ in source.calls),
        )
        self.assertEqual(result["coverage"]["reports_unincorporated"], 0)
        self.assertGreater(result["counters"]["map_calls"], 500)
        self.assertLess(result["counters"]["reduce_calls"], result["counters"]["map_calls"] // 2)
        self.assertGreater(result["counters"]["reduce_calls"], 1)
        self.assertTrue(all(call["size"] <= MAX_PROMPT_BYTES for call in model.calls))
        self.assertTrue(all(size <= MAX_REPORT_BYTES for size in model.output_sizes))
        self.assertFalse(any(call["raw_in_reducer"] for call in model.calls))
        self.assertLessEqual(result["counters"]["model_input_bytes"], AnalysisLimits().max_model_input_bytes)
        self.assertLessEqual(result["counters"]["model_output_bytes"], AnalysisLimits().max_model_output_bytes)
        self.assertEqual(result["counters"]["model_output_budget_bytes"], result["counters"]["model_output_bytes"])

    async def test_more_than_one_megabyte_single_file_is_not_a_single_context(self):
        text = (RAW_MARKER + "z" * 4_096) * 256
        self.assertGreater(len(text.encode()), 1_024 * 1_024)
        result, _, model = await self.run_analysis(FakeSource({"large.py": text}, final_json_limit=True))
        self.assertEqual(result["status"], "complete")
        self.assertGreater(result["counters"]["map_calls"], 256)
        self.assertEqual(result["coverage"]["files_reviewed"], 1)
        self.assertEqual(result["coverage"]["chunks_discovered"], result["coverage"]["chunks_reviewed"])
        self.assertFalse(any(call["raw_in_reducer"] for call in model.calls))

    async def test_long_escaped_unicode_is_split_after_framing_without_losing_text(self):
        text = ("\x00\"\\\n😀é" * 4_000) + "tail"
        model = FakeModel(capture=True)
        result, _, model = await self.run_analysis(
            FakeSource({"escaped.py": text}), model, focus='Inspect escaping: "\\\\", not just character counts.',
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual("".join(model.delivered.values()), text)
        self.assertGreater(result["counters"]["map_calls"], result["coverage"]["chunks_discovered"])
        self.assertEqual(result["coverage"]["fragments_reviewed"], result["counters"]["map_calls"])
        self.assertLessEqual(max(call["size"] for call in model.calls), MAX_PROMPT_BYTES)
        self.assertEqual(result["coverage"]["chunks_discovered"], result["coverage"]["chunks_reviewed"])

    async def test_lossless_replacement_parts_preserve_their_actual_sides_and_ranges(self):
        def mutate(name, arguments, response):
            if name == "read_diff_chunk":
                if "cursor" in arguments:
                    response.update(
                        representation="replacement-new", old_start=0, old_end=0,
                        new_start=5, new_end=5,
                    )
                else:
                    response.update(
                        representation="replacement-old", old_start=5, old_end=5,
                        new_start=0, new_end=0,
                    )
            return response

        result, _, model = await self.run_analysis(
            FakeSource({"file.py": "oldnew"}, chunk_size=3, mutate=mutate),
            FakeModel(capture=True),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["chunks_reviewed"], 2)
        self.assertEqual("".join(model.delivered.values()), "oldnew")
        for call in model.calls:
            if call["phase"] == "map":
                instructions = call["prompt"]["instructions"]
                self.assertIn("'replacement-old' and 'replacement-new'", instructions)
                self.assertIn("coarse lossless before/after snapshot blocks", instructions)
                self.assertIn("not unified hunks or proof", instructions)
                self.assertIn("removed or every new line added", instructions)
                self.assertIn("only from observed comparative evidence", instructions)
                self.assertIn("use needs_context for a bounded base/head range", instructions)
        citations = result["findings"][0]["citations"]
        self.assertEqual({item["representation"] for item in citations}, {"replacement-old", "replacement-new"})
        for item in citations:
            old_range = (item["old_start"], item["old_end"])
            new_range = (item["new_start"], item["new_end"])
            self.assertEqual(
                (old_range, new_range),
                ((5, 5), (0, 0)) if item["representation"] == "replacement-old" else ((0, 0), (5, 5)),
            )

    async def test_replacement_snapshot_can_request_the_pinned_opposite_side(self):
        def source_mutation(name, arguments, response):
            if name == "read_diff_chunk":
                response.update(representation="replacement-old", new_start=0, new_end=0)
            return response

        def model_mutation(phase, document, report, call_number):
            if phase == "map":
                self.assertEqual(document["chunks"][0]["representation"], "replacement-old")
                report["needs_context"] = [context_request(
                    path="file.py", side="head", start_line=5, end_line=10,
                    reason="Compare the opposite version before claiming removal.",
                )]
            return report

        result, source, _ = await self.run_analysis(
            FakeSource(mutate=source_mutation), FakeModel(mutate=model_mutation),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["context_requests_resolved"], 1)
        followups = [arguments for name, arguments in source.calls if name == "read_file_range"]
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0]["sha"], HEAD)
        self.assertEqual(followups[0]["path"], "file.py")
        self.assertEqual((followups[0]["start_line"], followups[0]["end_line"]), (5, 10))

    async def test_inventory_cursor_cycles_and_snapshot_changes_never_complete(self):
        for problem in ("cycle", "snapshot", "duplicate", "repository", "complete"):
            def mutate(name, arguments, response):
                if name == "list_change_files" and "cursor" in arguments:
                    if problem == "cycle":
                        response["next_cursor"] = arguments["cursor"]
                        response["complete"] = False
                    elif problem == "snapshot":
                        response["snapshot"]["head_sha"] = "e" * 40
                    elif problem == "duplicate":
                        response["files"][0]["path"] = "one.py"
                    elif problem == "repository":
                        response["repository"] = "other/project"
                    else:
                        response["complete"] = True
                        response["next_cursor"] = "still-pending"
                return response

            with self.subTest(problem=problem):
                result, _, _ = await self.run_analysis(
                    FakeSource({"one.py": "one", "two.py": "two", "three.py": "three"}, page_size=1, mutate=mutate),
                    limits=replace(AnalysisLimits(), concurrency=1),
                )
                self.assertEqual(result["status"], "partial")
                self.assertFalse(result["coverage"]["inventory_complete"])
                self.assertEqual(result["coverage"]["files_reviewed"], 1)
                self.assertGreater(result["coverage"]["unresolved_count"], 0)

    async def test_diff_cursor_cycles_and_repeated_chunk_ids_are_detected(self):
        for problem in ("cursor", "id"):
            def mutate(name, arguments, response):
                if name == "read_diff_chunk":
                    if problem == "id":
                        response["chunk_id"] = "same-id"
                    elif "cursor" in arguments:
                        response["next_cursor"] = arguments["cursor"]
                        response["complete"] = False
                return response

            with self.subTest(problem=problem):
                result, _, _ = await self.run_analysis(
                    FakeSource({"file.py": "x" * 40}, chunk_size=10, mutate=mutate),
                )
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["coverage"]["chunks_reviewed"], 1)
                self.assertEqual(result["coverage"]["files_reviewed"], 0)
                code = "cursor_cycle" if problem == "cursor" else "chunk_id_repeated"
                self.assertIn(code, result["coverage"]["unresolved_by_code"])

    async def test_invalid_or_mismatched_diff_metadata_is_not_analyzed(self):
        mutations = (
            {"repository": "other/project"}, {"path": "other.py"},
            {"snapshot_id": "changed"}, {"old_blob_sha": "e" * 40},
            {"new_blob_sha": None}, {"new_start": True},
            {"old_start": 4, "old_end": 3}, {"complete": False, "next_cursor": None},
            {"content": ""}, {"content": "x" * (MAX_CHUNK_BYTES + 1)},
        )
        for change in mutations:
            def mutate(name, arguments, response):
                if name == "read_diff_chunk":
                    response.update(change)
                return response

            with self.subTest(change=tuple(change)):
                result, _, model = await self.run_analysis(FakeSource(mutate=mutate))
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["coverage"]["chunks_reviewed"], 0)
                self.assertEqual(result["coverage"]["files_reviewed"], 0)
                self.assertEqual(model.calls, [])

    async def test_missing_patch_and_binary_files_are_unresolved_not_unchanged(self):
        for status in ("binary", "unsupported"):
            def mutate(name, arguments, response):
                if name == "read_diff_chunk" and arguments["path"] == "asset.bin":
                    response.update(status=status, content="", reason="Explicit unsupported representation")
                return response

            with self.subTest(status=status):
                result, _, _ = await self.run_analysis(
                    FakeSource({"good.py": "+valid", "asset.bin": "binary"}, mutate=mutate),
                )
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["coverage"]["files_discovered"], 2)
                self.assertEqual(result["coverage"]["files_reviewed"], 1)
                self.assertEqual(result["coverage"]["unresolved_by_code"], {"source_" + status: 1})
                self.assertNotIn("no-change", json.dumps(result))

    async def test_bad_model_reports_retry_once_without_fabricated_coverage(self):
        invalid_outputs = (
            None, "", "not json", {}, "{}", '{"summary":"x","summary":"y"}',
            {"reviewed_chunks": ["fake"], "summary": "x", "findings": [], "needs_context": []},
            {"reviewed_chunks": ["d0.0.0"], "summary": "x", "findings": [{"text": "x", "evidence": ["fake"]}], "needs_context": []},
            {"reviewed_chunks": ["d0.0.0", "d0.0.0"], "summary": "x", "findings": [], "needs_context": []},
            {"reviewed_chunks": ["d0.0.0"], "summary": "x" * 600, "findings": [], "needs_context": []},
            "x" * (MAX_REPORT_BYTES + 1),
        )
        for output in invalid_outputs:
            with self.subTest(output_type=type(output).__name__, output_length=len(str(output))):
                model = FakeModel(mutate=lambda *args: output)
                result, _, model = await self.run_analysis(model=model)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["counters"]["model_calls"], 2)
                self.assertEqual(result["counters"]["model_retries"], 1)
                self.assertEqual(result["coverage"]["fragments_delivered"], 1)
                self.assertEqual(result["coverage"]["chunks_reviewed"], 0)
                self.assertEqual(result["coverage"]["unresolved_count"], 1)
                self.assertEqual(result["findings"], [])

    async def test_recovered_retry_counts_actual_calls_but_not_duplicate_coverage(self):
        def mutate(phase, document, report, call_number):
            return "" if call_number == 0 else report

        result, _, _ = await self.run_analysis(model=FakeModel(mutate=mutate))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counters"]["model_calls"], 2)
        self.assertEqual(result["counters"]["model_retries"], 1)
        self.assertEqual(result["coverage"]["fragments_delivered"], 1)
        self.assertEqual(result["coverage"]["chunks_reviewed"], 1)
        self.assertEqual(result["coverage"]["reports_produced"], 1)

    async def test_callback_exceptions_are_sanitized(self):
        def source_failure(*args):
            raise RuntimeError(SECRET)

        result, _, _ = await self.run_analysis(FakeSource(mutate=source_failure))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["coverage"]["unresolved_by_code"], {"source_error": 1})

        def model_failure(*args):
            raise RuntimeError(SECRET)

        result, _, _ = await self.run_analysis(model=FakeModel(mutate=model_failure))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["counters"]["model_errors"], 2)
        self.assertEqual(result["coverage"]["unresolved_by_code"], {"model_error": 1})
        self.assertEqual(result["counters"]["model_output_budget_bytes"], 2 * MAX_REPORT_BYTES)

    async def test_unknown_output_from_failed_calls_consumes_cumulative_budget(self):
        def model_failure(*args):
            raise RuntimeError(SECRET)

        result, _, _ = await self.run_analysis(
            model=FakeModel(mutate=model_failure),
            limits=replace(AnalysisLimits(), max_model_output_bytes=MAX_REPORT_BYTES),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["counters"]["model_calls"], 1)
        self.assertEqual(result["counters"]["model_retries"], 0)
        self.assertEqual(result["counters"]["model_output_bytes"], 0)
        self.assertEqual(result["counters"]["model_output_budget_bytes"], MAX_REPORT_BYTES)
        self.assertIn("model_output_byte_limit", result["coverage"]["unresolved_by_code"])

    async def test_known_output_refunds_unused_reservation_within_cumulative_budget(self):
        result, _, model = await self.run_analysis(
            FakeSource({"one.py": "one", "two.py": "two", "three.py": "three"}),
            limits=replace(
                AnalysisLimits(), concurrency=1, max_report_bytes=512, max_model_output_bytes=1_400,
            ),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counters"]["model_calls"], 4)
        self.assertEqual(result["counters"]["model_output_bytes"], sum(model.output_sizes))
        self.assertEqual(result["counters"]["model_output_budget_bytes"], sum(model.output_sizes))
        self.assertLessEqual(result["counters"]["model_output_budget_bytes"], 1_400)

    async def test_reducers_cannot_cite_ledger_ids_absent_from_their_inputs(self):
        def mutate(phase, document, report, call_number):
            if phase != "reduce":
                report["findings"] = []
            else:
                report["findings"] = [{"text": "Fabricated carry-through", "evidence": ["d0.0.0"]}]
            return report

        result, _, model = await self.run_analysis(
            FakeSource({"first.py": "first", "second.py": "second"}), FakeModel(mutate=mutate),
        )
        self.assertEqual(result["status"], "partial")
        self.assertIn("reduction_invalid_model_report", result["coverage"]["unresolved_by_code"])
        self.assertEqual(result["coverage"]["chunks_reviewed"], 2)
        self.assertEqual(result["counters"]["reduce_calls"], 2)
        self.assertEqual(len(model.calls), 4)
        self.assertGreater(result["coverage"]["reports_unincorporated"], 0)

    async def test_uncompressible_reducer_response_fails_instead_of_looping(self):
        def mutate(phase, document, report, call_number):
            if phase == "reduce":
                report["summary"] = "uncompressible" * 1_000
            return report

        result, _, _ = await self.run_analysis(
            FakeSource({"file.py": "x" * 30}, chunk_size=10), FakeModel(mutate=mutate),
            limits=replace(AnalysisLimits(), concurrency=1),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counters"]["reduce_calls"], 2)
        self.assertEqual(result["counters"]["reductions"], 0)
        self.assertIn("reduction_model_report_limit", result["coverage"]["unresolved_by_code"])
        self.assertEqual(result["coverage"]["chunks_reviewed"], 3)
        self.assertGreater(result["coverage"]["reports_unincorporated"], 0)

    async def test_reduction_prompt_limit_is_explicit_and_never_discards_evidence_silently(self):
        def mutate(phase, document, report, call_number):
            if phase == "map":
                report["summary"] = "s" * 500
                report["findings"] = [
                    {"text": "x" * 240, "evidence": report["reviewed_chunks"]} for _ in range(4)
                ]
            return report

        result, _, model = await self.run_analysis(
            FakeSource({"one.py": "one", "two.py": "two"}), FakeModel(mutate=mutate),
            limits=replace(AnalysisLimits(), max_prompt_bytes=3_500),
        )
        self.assertEqual(result["status"], "partial")
        self.assertIn("reduction_prompt_limit", result["coverage"]["unresolved_by_code"])
        self.assertTrue(all(call["size"] <= 3_500 for call in model.calls))
        self.assertEqual(result["counters"]["reduce_calls"], 0)

    async def test_finite_count_and_byte_budgets_stop_with_honest_coverage(self):
        for change, code in (
            ({"max_files": 1}, "file_limit"),
            ({"max_chunks": 1}, "chunk_limit"),
            ({"max_source_calls": 1}, "source_call_limit"),
            ({"max_model_calls": 1}, "model_call_limit"),
            ({"max_source_bytes": 1}, "source_byte_limit"),
            ({"max_model_input_bytes": 1}, "model_input_byte_limit"),
            ({"max_model_output_bytes": 1}, "model_output_byte_limit"),
        ):
            with self.subTest(change=change):
                result, _, _ = await self.run_analysis(
                    FakeSource({"one.py": "one", "two.py": "two"}),
                    limits=replace(AnalysisLimits(), concurrency=1, **change),
                )
                self.assertNotEqual(result["status"], "complete")
                self.assertIn(code, result["coverage"]["unresolved_by_code"])
                self.assertLessEqual(result["coverage"]["chunks_reviewed"], result["coverage"]["chunks_discovered"])

    async def test_context_is_fetched_at_a_pinned_sha_and_analyzed_separately(self):
        def mutate(phase, document, report, call_number):
            if phase == "map":
                report["needs_context"] = [context_request()]
            return report

        result, source, model = await self.run_analysis(model=FakeModel(mutate=mutate, capture=True))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["context_requests"], 1)
        self.assertEqual(result["coverage"]["context_requests_resolved"], 1)
        self.assertEqual(result["coverage"]["context_chunks_reviewed"], 1)
        self.assertEqual(result["counters"]["context_calls"], 1)
        requests = [arguments for name, arguments in source.calls if name == "read_file_range"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0], {
            "repository": REPOSITORY, "path": "implementation.py", "sha": BASE,
            "start_line": 1, "end_line": 10, "max_bytes": MAX_CHUNK_BYTES,
        })
        context_prompts = [call["prompt"] for call in model.calls if call["phase"] == "context"]
        self.assertEqual(len(context_prompts), 1)
        self.assertNotIn("+new source", json.dumps(context_prompts[0]))
        citations = [citation for finding in result["findings"] for citation in finding["citations"]]
        self.assertTrue(any(citation.get("sha") == BASE and citation["kind"] == "context" for citation in citations))

    async def test_recursive_context_requests_are_bounded_and_report_unresolved(self):
        def mutate(phase, document, report, call_number):
            if phase != "reduce":
                report["needs_context"] = [context_request()]
            return report

        result, _, _ = await self.run_analysis(model=FakeModel(mutate=mutate))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["context_requests"], 2)
        self.assertEqual(result["coverage"]["context_requests_resolved"], 1)
        self.assertEqual(result["coverage"]["context_requests_unresolved"], 1)
        self.assertEqual(result["counters"]["context_calls"], 1)
        self.assertIn("context_round_limit", result["coverage"]["unresolved_by_code"])

    async def test_context_request_schema_and_ranges_are_not_trusted(self):
        for changes in (
            {"path": "../outside"}, {"side": "current"}, {"start_line": 0},
            {"start_line": True}, {"end_line": 1_000}, {"start_line": 20, "end_line": 10},
            {"reason": "x" * 200},
        ):
            def mutate(phase, document, report, call_number):
                report["needs_context"] = [context_request(**changes)]
                return report

            with self.subTest(changes=tuple(changes)):
                result, source, _ = await self.run_analysis(model=FakeModel(mutate=mutate))
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["coverage"]["chunks_reviewed"], 0)
                self.assertFalse(any(name == "read_file_range" for name, _ in source.calls))

    async def test_context_response_identity_ranges_and_cursors_are_validated(self):
        def model_mutation(phase, document, report, call_number):
            if phase == "map":
                report["needs_context"] = [context_request()]
            return report

        for change in (
            {"sha": HEAD}, {"path": "elsewhere.py"}, {"end_line": 100},
            {"start_line": 2, "end_line": 2}, {"complete": False, "next_cursor": None},
        ):
            def source_mutation(name, arguments, response):
                if name == "read_file_range":
                    response.update(change)
                return response

            with self.subTest(change=change):
                result, _, _ = await self.run_analysis(
                    FakeSource(mutate=source_mutation), FakeModel(mutate=model_mutation),
                )
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["coverage"]["chunks_reviewed"], 1)
                self.assertEqual(result["coverage"]["context_requests_unresolved"], 1)
                self.assertEqual(result["counters"]["context_calls"], 0)

    async def test_context_limits_apply_across_reused_worker_slots(self):
        def mutate(phase, document, report, call_number):
            if phase == "map":
                report["needs_context"] = [context_request()]
            return report

        for changes in (
            {"max_context_requests": 1},
            {"max_context_requests_per_worker": 1},
        ):
            with self.subTest(changes=changes):
                result, _, _ = await self.run_analysis(
                    FakeSource({f"file-{index}.py": "source" for index in range(3)}),
                    FakeModel(mutate=mutate),
                    limits=replace(AnalysisLimits(), concurrency=1, **changes),
                )
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["coverage"]["context_requests"], 3)
                self.assertEqual(result["coverage"]["context_requests_resolved"], 1)
                self.assertEqual(result["coverage"]["context_requests_unresolved"], 2)
                self.assertEqual(result["coverage"]["unresolved_by_code"]["context_request_limit"], 2)

    async def test_context_chunk_budget_stops_with_unresolved_request(self):
        def mutate(phase, document, report, call_number):
            if phase == "map":
                report["needs_context"] = [context_request()]
            return report

        result, _, _ = await self.run_analysis(
            FakeSource({"file.py": "source"}, chunk_size=10), FakeModel(mutate=mutate),
            limits=replace(AnalysisLimits(), max_context_chunks=1),
        )
        self.assertEqual(result["status"], "partial")
        self.assertIn("context_chunk_limit", result["coverage"]["unresolved_by_code"])
        self.assertEqual(result["coverage"]["context_requests_unresolved"], 1)
        self.assertEqual(result["coverage"]["chunks_reviewed"], 1)

    async def test_context_cursor_cycles_preserve_pending_evidence(self):
        def source_mutation(name, arguments, response):
            if name == "read_file_range" and "cursor" in arguments:
                response.update(next_cursor=arguments["cursor"], complete=False)
            return response

        def model_mutation(phase, document, report, call_number):
            if phase == "map":
                report["needs_context"] = [context_request()]
            return report

        result, _, _ = await self.run_analysis(
            FakeSource({"file.py": "source"}, chunk_size=10, mutate=source_mutation),
            FakeModel(mutate=model_mutation),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["chunks_reviewed"], 1)
        self.assertEqual(result["coverage"]["context_requests_unresolved"], 1)
        self.assertEqual(result["coverage"]["context_chunks_reviewed"], 1)
        self.assertIn("cursor_cycle", result["coverage"]["unresolved_by_code"])

    async def test_source_envelope_bytes_and_types_are_bounded_before_use(self):
        for output in (
            [], {"arbitrary": "x" * 140_000}, {"arbitrary": float("nan")},
            {"repository": REPOSITORY, "snapshot": {}},
        ):
            async def malformed_source(name, arguments):
                return output

            with self.subTest(output_type=type(output).__name__):
                result, _, model = await self.run_analysis(malformed_source)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(model.calls, [])
                self.assertLessEqual(result["counters"]["source_bytes"], AnalysisLimits().max_source_bytes)

    async def test_reducer_retry_does_not_double_count_leaf_coverage(self):
        rejected = False

        def mutate(phase, document, report, call_number):
            nonlocal rejected
            if phase == "reduce" and not rejected:
                rejected = True
                return ""
            return report

        result, _, _ = await self.run_analysis(
            FakeSource({"first.py": "one", "second.py": "two"}), FakeModel(mutate=mutate),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["chunks_reviewed"], 2)
        self.assertEqual(result["coverage"]["reports_produced"], 2)
        self.assertEqual(result["coverage"]["reports_in_summary"], 2)
        self.assertEqual(result["counters"]["model_calls"], 4)
        self.assertEqual(result["counters"]["reduce_calls"], 2)
        self.assertEqual(result["counters"]["reductions"], 1)
        self.assertEqual(result["counters"]["model_retries"], 1)

    async def test_cancellation_propagates_and_cancels_all_active_model_callbacks(self):
        started = asyncio.Event()
        active = 0
        cancelled = 0

        async def blocked_model(phase, prompt):
            nonlocal active, cancelled
            active += 1
            if active == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                cancelled += 1

        task = asyncio.create_task(analyze_change(
            FakeSource({"one.py": "one", "two.py": "two"}), blocked_model,
            repository=REPOSITORY, pull_number=1,
        ))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(active, 0)
        self.assertEqual(cancelled, 2)

    async def test_deadline_covers_and_cancels_source_callbacks(self):
        source = FakeSource({"one.py": "one", "two.py": "two"})
        active = 0
        cancelled = 0

        async def blocked_source(name, arguments):
            nonlocal active, cancelled
            if name == "list_change_files":
                return await source(name, arguments)
            active += 1
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                cancelled += 1

        result = await asyncio.wait_for(analyze_change(
            blocked_source, FakeModel(), repository=REPOSITORY, pull_number=1,
            limits=replace(AnalysisLimits(), max_seconds=0.04),
        ), 2)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("deadline_exceeded", result["coverage"]["unresolved_by_code"])
        self.assertEqual(active, 0)
        self.assertEqual(cancelled, 2)

    async def test_same_deadline_covers_map_context_and_reduction(self):
        for blocked_phase in ("map", "context", "reduce"):
            cancelled = []

            def request_context(phase, document, report, call_number):
                if blocked_phase == "context" and phase == "map":
                    report["needs_context"] = [context_request()]
                return report

            model = FakeModel(mutate=request_context)

            async def blocked_model(phase, prompt):
                if phase == blocked_phase:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.append(phase)
                return await model(phase, prompt)

            with self.subTest(phase=blocked_phase):
                result = await asyncio.wait_for(analyze_change(
                    FakeSource({"one.py": "source", "two.py": "source"}), blocked_model,
                    repository=REPOSITORY, pull_number=1,
                    limits=replace(AnalysisLimits(), max_seconds=0.04),
                ), 2)
                self.assertNotEqual(result["status"], "complete")
                self.assertIn("deadline_exceeded", result["coverage"]["unresolved_by_code"])
                self.assertGreater(len(cancelled), 0)
                self.assertEqual(result["counters"]["model_errors"], 0)
                self.assertEqual(
                    result["counters"]["model_output_budget_bytes"] - result["counters"]["model_output_bytes"],
                    len(cancelled) * MAX_REPORT_BYTES,
                )

    async def test_deadline_includes_async_progress_and_does_not_start_io(self):
        cancelled = []

        async def blocked_progress(value):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        result, source, model = await self.run_analysis(
            on_progress=blocked_progress,
            limits=replace(AnalysisLimits(), max_seconds=0.02),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("deadline_exceeded", result["coverage"]["unresolved_by_code"])
        self.assertEqual(source.calls, [])
        self.assertEqual(model.calls, [])
        self.assertEqual(cancelled, [True])

    async def test_concurrency_bounds_both_kinds_of_callbacks_and_task_allocation(self):
        activity = Activity()
        source = FakeSource({f"file-{index}.py": "source" for index in range(100)}, activity=activity)
        model = FakeModel(activity=activity)
        result, _, _ = await self.run_analysis(source, model)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(activity.active, 0)
        self.assertEqual(activity.peak, 2)
        self.assertLessEqual(activity.task_peak, 2)

    async def test_reduction_and_coverage_are_deterministic_under_different_completion_order(self):
        data = {f"file-{index}.py": "source" * 4 for index in range(7)}
        result_one, _, _ = await self.run_analysis(FakeSource(data, chunk_size=10), FakeModel())
        result_two, _, _ = await self.run_analysis(FakeSource(data, chunk_size=10), FakeModel(reorder=True))
        self.assertEqual(result_one["status"], "complete")
        self.assertEqual(result_one, result_two)

    async def test_multiway_reduction_reduces_calls_without_assembling_raw_diffs(self):
        result, _, model = await self.run_analysis(
            FakeSource({f"file-{index}.py": RAW_MARKER for index in range(64)}),
            FakeModel(capture=True),
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["chunks_reviewed"], 64)
        self.assertEqual(result["coverage"]["reports_in_summary"], 64)
        self.assertEqual(result["counters"]["map_calls"], 64)
        self.assertEqual(result["counters"]["reduce_calls"], 9)
        self.assertEqual(result["counters"]["model_calls"], 73)
        reductions = [call for call in model.calls if call["phase"] == "reduce"]
        self.assertTrue(all(len(call["prompt"]["reports"]) == MAX_REDUCTION_FAN_IN for call in reductions))
        self.assertTrue(all(call["size"] <= MAX_PROMPT_BYTES for call in reductions))
        self.assertFalse(any(call["raw_in_reducer"] for call in reductions))
        for call in reductions:
            self.assertTrue(all(set(report) == {"summary", "findings"} for report in call["prompt"]["reports"]))

    async def test_multiway_reduction_falls_back_to_smaller_groups_by_actual_bytes(self):
        def large_reports(phase, document, report, call_number):
            report["summary"] = "s" * 500
            evidence = report["findings"][0]["evidence"]
            report["findings"] = [{"text": "f" * 240, "evidence": evidence} for _ in range(4)]
            return report

        result, _, model = await self.run_analysis(
            FakeSource({f"file-{index}.py": "source" for index in range(24)}),
            FakeModel(capture=True, mutate=large_reports),
            focus="Examine the requested change. " + "x" * 700,
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["reports_in_summary"], 24)
        reductions = [call for call in model.calls if call["phase"] == "reduce"]
        self.assertGreater(len(reductions), 1)
        self.assertTrue(all(2 <= len(call["prompt"]["reports"]) < MAX_REDUCTION_FAN_IN for call in reductions))
        self.assertTrue(all(call["size"] <= MAX_PROMPT_BYTES for call in reductions))
        self.assertTrue(all(size <= MAX_REPORT_BYTES for size in model.output_sizes))

    async def test_progress_is_sanitized_and_observability_failures_do_not_discard_analysis(self):
        progress = []

        async def record(value):
            progress.append(value)
            raise RuntimeError(SECRET)

        result, _, _ = await self.run_analysis(on_progress=record)
        self.assertEqual(result["status"], "complete")
        self.assertGreater(result["counters"]["progress_errors"], 0)
        self.assertTrue(all(set(event) == {"phase", "source_calls", "model_calls", "chunks_reviewed"} for event in progress))

    async def test_final_result_byte_limit_explicitly_withholds_findings_without_losing_counts(self):
        data = {("path-" + str(index) + "-" + "x" * 800 + ".py"): "source" for index in range(3)}
        result, _, _ = await self.run_analysis(
            FakeSource(data), limits=replace(AnalysisLimits(), max_result_bytes=4_096),
        )
        self.assertEqual(result["status"], "partial")
        coverage = result["coverage"]
        self.assertTrue(coverage["output_limited"])
        self.assertIn("result_size_limit", coverage["unresolved_by_code"])
        self.assertEqual(coverage["chunks_reviewed"], 3)
        self.assertEqual(coverage["reports_produced"], 3)
        self.assertEqual(coverage["reports_unincorporated"], 3)
        self.assertEqual(coverage["findings_omitted"], 1)
        self.assertEqual(coverage["citations_omitted"], 3)
        self.assertEqual(result["findings"], [])
        self.assertEqual(coverage["unresolved_omitted"], coverage["unresolved_count"])

    async def test_unresolved_records_are_bounded_but_counts_are_not_silently_truncated(self):
        def mutate(name, arguments, response):
            if name == "read_diff_chunk":
                response.update(status="binary", reason="Not text", content="")
            return response

        result, _, _ = await self.run_analysis(
            FakeSource({f"asset-{index}.bin": "binary" for index in range(20)}, mutate=mutate),
            limits=replace(AnalysisLimits(), max_unresolved_records=3),
        )
        self.assertEqual(result["status"], "blocked")
        coverage = result["coverage"]
        self.assertEqual(coverage["unresolved_count"], 20)
        self.assertEqual(coverage["unresolved_by_code"], {"source_binary": 20})
        self.assertEqual(len(coverage["unresolved"]), 3)
        self.assertEqual(coverage["unresolved_omitted"], 17)
        self.assertEqual(coverage["files_incomplete"], 20)


if __name__ == "__main__":
    unittest.main()
