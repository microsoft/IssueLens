import copy
import json
import pathlib
import unittest
from unittest.mock import patch

import test_team_memory_workflow as memory_tests


action = memory_tests.action
Response = memory_tests.Response


class PushBatchTests(unittest.TestCase):
    execute = memory_tests.TeamMemoryActionTests.execute
    write_envelope = memory_tests.TeamMemoryActionTests.write_envelope
    action_outputs = memory_tests.TeamMemoryActionTests.action_outputs
    prepared_envelope = memory_tests.TeamMemoryActionTests.prepared_envelope
    stream = memory_tests.TeamMemoryActionTests.stream

    def setUp(self):
        memory_tests.TeamMemoryActionTests.setUp(self)
        self.before = "d" * 40
        self.after = "e" * 40
        self.commits = [self.merge_sha, self.after]
        self.environment.update(GITHUB_EVENT_NAME="push", GITHUB_SHA=self.after)
        self.event = {
            "repository": self.project, "ref": "refs/heads/main",
            "before": self.before, "after": self.after,
            "created": False, "deleted": False, "forced": False,
            "commits": [{"id": sha, "message": "UNTRUSTED_COMMIT_TEXT"} for sha in self.commits],
            "head_commit": {"id": self.after},
            "compare": "https://untrusted.test/do-not-fetch",
        }
        self.comparison = {
            "base_commit": {"sha": self.before}, "merge_base_commit": {"sha": self.before},
            "status": "ahead", "ahead_by": 2, "behind_by": 0, "total_commits": 2,
            "commits": [{"sha": self.after}],
        }
        self.pulls = [self.pull_node(27, self.merge_sha), self.pull_node(28, self.after)]
        self.envelope["metadata"] = {
            "repository": "example/project", "event_name": "push",
            "push_before": self.before, "push_after": self.after,
            "pull_requests": [
                {"pull_number": item["number"], "merge_commit_sha": item["mergeCommit"]["oid"]}
                for item in self.pulls
            ],
        }
        self.result = {
            "status": "updated", "source_repository": "example/project",
            "push_before": self.before, "push_after": self.after,
            "wiki_repository": "example/knowledge", "wiki_sha": "c" * 40,
            "reason": "Confirmed publication of the verified batch.",
            "results": [
                {**item, "status": "updated", "reason": "Verified and published."}
                for item in self.envelope["metadata"]["pull_requests"]
            ],
        }

    def pull_node(self, number, sha, **changes):
        return {
            "number": number, "state": "MERGED", "merged": True,
            "mergedAt": "2026-09-21T00:00:00Z", "baseRefName": "main",
            "baseRepository": {"databaseId": 100, "nameWithOwner": "example/project"},
            "mergeCommit": {"oid": sha}, **changes,
        }

    def association_response(self, shas=None, pulls=None):
        repository = {
            "databaseId": 100, "nameWithOwner": "example/project",
            "defaultBranchRef": {"name": "main", "target": {"oid": self.after}},
        }
        for index, sha in enumerate(shas or self.commits):
            nodes = copy.deepcopy(self.pulls if pulls is None else pulls)
            repository[f"c{index}"] = {
                "oid": sha,
                "associatedPullRequests": {
                    "totalCount": len(nodes), "pageInfo": {"hasNextPage": False}, "nodes": nodes,
                },
            }
        return {"data": {"repository": repository}}

    def responses(self, association=None, comparison=None):
        return [
            Response(json.dumps(value).encode())
            for value in (self.project, comparison or self.comparison, association or self.association_response())
        ]

    def test_one_request_contains_deduplicated_verified_prs_not_raw_evidence(self):
        self.execute("preflight", self.responses())
        envelope = self.prepared_envelope()
        metadata = envelope["metadata"]
        self.assertEqual(metadata["push_before"], self.before)
        self.assertEqual(metadata["push_after"], self.after)
        self.assertEqual([item["pull_number"] for item in metadata["pull_requests"]], [27, 28])
        self.assertEqual(metadata["commit_count"], 2)
        text = envelope["request"]["input"]
        self.assertIn("independent", text)
        self.assertIn("partial", text)
        self.assertIn("dependencies", text)
        self.assertIn("final source state", text)
        self.assertIn("sequentially", text)
        self.assertIn("example/project#27:" + self.merge_sha, text)
        self.assertIn("example/project#28:" + self.after, text)
        self.assertNotIn("UNTRUSTED", text)
        self.assertNotIn("untrusted.test", text)
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertIn("?per_page=1&page=2", requests[1].full_url)
        self.assertEqual(requests[2].full_url, "https://api.github.com/graphql")
        query = json.loads(requests[2].data)
        self.assertIn("associatedPullRequests", query["query"])
        self.assertNotIn("body", query["query"])
        self.token.assert_not_called()

    def test_empty_eligible_pr_set_skips_before_azure_login(self):
        nodes = [
            self.pull_node(27, "f" * 40),
            self.pull_node(28, self.after, baseRefName="release"),
            self.pull_node(29, self.after, state="OPEN", merged=False, mergedAt=None, mergeCommit=None),
        ]
        self.execute("preflight", self.responses(self.association_response(pulls=nodes)))
        self.assertEqual(self.action_outputs()["skip-reason"], "no_merged_pull_requests")
        self.assertEqual(self.action_outputs()["eligible"], "false")
        self.token.assert_not_called()

    def test_single_squash_merge_and_multicommit_rebase_use_merge_identity(self):
        for commits in ([self.after], ["f" * 40, self.merge_sha, self.after]):
            with self.subTest(commits=commits):
                self.event["commits"] = [{"id": sha} for sha in commits]
                comparison = {
                    **self.comparison, "total_commits": len(commits), "ahead_by": len(commits),
                    "commits": [{"sha": commits[1]}] if len(commits) > 1 else [],
                }
                node = self.pull_node(28, self.after)
                self.execute("preflight", self.responses(
                    self.association_response(shas=commits, pulls=[node]), comparison,
                ))
                self.assertEqual(
                    self.prepared_envelope()["metadata"]["pull_requests"][0]["merge_commit_sha"], self.after,
                )
                self.assertEqual(len(self.prepared_envelope()["metadata"]["pull_requests"]), 1)

    def test_multiple_lookup_groups_do_not_duplicate_the_pr(self):
        commits = [f"{index:040x}" for index in range(1, 22)]
        self.after = commits[-1]
        self.environment["GITHUB_SHA"] = self.after
        self.event.update(after=self.after, head_commit={"id": self.after},
                          commits=[{"id": sha} for sha in commits])
        comparison = {**self.comparison, "ahead_by": 21, "total_commits": 21,
                      "commits": [{"sha": commits[1]}]}
        pulls = [self.pull_node(27, self.after)]
        responses = [
            self.project, comparison,
            self.association_response(commits[:20], pulls),
            self.association_response(commits[20:], pulls),
        ]
        self.execute("preflight", [Response(json.dumps(value).encode()) for value in responses])
        self.assertEqual(self.opener.open.call_count, 4)
        metadata = self.prepared_envelope()["metadata"]
        self.assertEqual(metadata["commit_count"], 21)
        self.assertEqual(len(metadata["pull_requests"]), 1)
        self.assertEqual(metadata["pull_requests"][0]["merge_commit_sha"], self.after)

    def test_one_commit_can_be_associated_with_multiple_merged_prs(self):
        self.event["commits"] = [{"id": self.after}]
        comparison = {**self.comparison, "ahead_by": 1, "total_commits": 1, "commits": []}
        pulls = [self.pull_node(number, self.after) for number in (27, 28)]
        self.execute("preflight", self.responses(self.association_response([self.after], pulls), comparison))
        self.assertEqual([item["pull_number"] for item in self.prepared_envelope()["metadata"]["pull_requests"]], [27, 28])

    def test_newer_default_branch_tip_is_handed_off_without_expanding_authorized_prs(self):
        response = self.association_response()
        response["data"]["repository"]["defaultBranchRef"]["target"]["oid"] = "f" * 40
        self.execute("preflight", self.responses(response))
        envelope = self.prepared_envelope()
        self.assertEqual(envelope["metadata"]["source_tip_sha"], "f" * 40)
        self.assertEqual(len(envelope["metadata"]["pull_requests"]), 2)
        self.assertIn("Other pushed commits do not", envelope["request"]["input"])
        self.assertIn("superseded changes", envelope["request"]["input"])

    def test_unsafe_or_malformed_pushes_fail_without_submission(self):
        original = copy.deepcopy(self.event)
        cases = [
            {"forced": True}, {"created": True}, {"deleted": True}, {"forced": None},
            {"before": "0" * 40}, {"after": "short"}, {"before": self.after},
            {"ref": "refs/heads/other"}, {"commits": []},
            {"commits": [{"id": self.after}, {"id": self.after}]},
            {"commits": [{"id": self.before}, {"id": self.after}]},
            {"commits": [{"id": "short"}]}, {"head_commit": {"id": self.before}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.event = {**copy.deepcopy(original), **changes}
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.responses())
                self.token.assert_not_called()
                self.assertFalse((self.directory / "output.txt").exists())

    def test_incomplete_or_different_comparison_fails_without_partial_discovery(self):
        for changes in (
            {"total_commits": 3}, {"total_commits": True}, {"ahead_by": 3},
            {"behind_by": 1}, {"status": "diverged"},
            {"base_commit": {"sha": "f" * 40}}, {"merge_base_commit": {"sha": "f" * 40}},
            {"commits": []}, {"commits": [{"sha": "f" * 40}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(SystemExit):
                self.execute("preflight", self.responses(comparison={**self.comparison, **changes}))
            self.assertFalse((self.directory / "output.txt").exists())
            self.token.assert_not_called()

    def test_incomplete_graphql_data_and_wrong_identities_fail_closed(self):
        for mutation in ("errors", "repository", "branch", "commit", "page", "count", "missing", "foreign", "changed"):
            response = self.association_response()
            repository = response["data"]["repository"]
            connection = repository["c0"]["associatedPullRequests"]
            if mutation == "errors":
                response["errors"] = [{"message": "PRIVATE DETAIL"}]
            elif mutation == "repository":
                repository["databaseId"] = 101
            elif mutation == "branch":
                repository["defaultBranchRef"]["name"] = "other"
            elif mutation == "commit":
                repository["c0"]["oid"] = self.before
            elif mutation == "page":
                connection["pageInfo"]["hasNextPage"] = True
            elif mutation == "count":
                connection["totalCount"] = 3
            elif mutation == "missing":
                repository["c1"] = None
            elif mutation == "foreign":
                connection["nodes"][0]["baseRepository"]["databaseId"] = 101
            else:
                repository["c1"]["associatedPullRequests"]["nodes"][0]["mergeCommit"]["oid"] = self.after
            with self.subTest(mutation=mutation), self.assertRaises(SystemExit) as raised:
                self.execute("preflight", self.responses(response))
            self.assertNotIn("PRIVATE DETAIL", str(raised.exception))
            self.assertFalse((self.directory / "output.txt").exists())
            self.token.assert_not_called()

    def test_discovery_limits_are_explicit_not_silent_truncation(self):
        for constant, value in (("MAX_PUSH_COMMITS", 1), ("MAX_BATCH_PRS", 1)):
            with self.subTest(constant=constant), patch.object(action, constant, value):
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.responses())
                self.assertFalse((self.directory / "output.txt").exists())
        with patch.object(action.time, "monotonic", side_effect=[0, 181]):
            with self.assertRaises(SystemExit):
                self.execute("preflight", self.responses())
            self.assertFalse((self.directory / "output.txt").exists())

    def test_batch_request_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            action.build_team_memory_request({"event_name": "push", "extra": "x" * (64 * 1024)})

    def test_complete_batch_records_all_prs_and_preserves_response(self):
        self.write_envelope()
        self.execute("submit", [self.stream()])
        outputs = self.action_outputs()
        self.assertEqual(outputs["status"], "updated")
        self.assertEqual(json.loads(pathlib.Path(outputs["response-path"]).read_text()), self.result)
        summary = (self.directory / "summary.md").read_text()
        self.assertIn("27", summary)
        self.assertIn("28", summary)
        self.assertIn(self.after, summary)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_partial_publication_retains_receipt_but_fails_the_job(self):
        self.result["status"] = "partial"
        self.result["results"][1].update(status="needs-review", reason="Missing evidence; depends on unverified work.")
        self.write_envelope()
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.execute("submit", [self.stream()])
        outputs = self.action_outputs()
        self.assertEqual(outputs["status"], "partial")
        self.assertEqual(outputs["wiki-sha"], self.result["wiki_sha"])
        self.assertEqual(json.loads(pathlib.Path(outputs["response-path"]).read_text()), self.result)
        summary = (self.directory / "summary.md").read_text()
        self.assertIn("incomplete", summary)
        self.assertIn("needs-review", summary)
        self.assertNotIn("batch completed", summary)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_no_change_subset_is_partial_without_claiming_publication(self):
        self.result["status"] = "partial"
        self.result["results"][0]["status"] = "no-change"
        self.result["results"][1]["status"] = "failed"
        self.write_envelope()
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.execute("submit", [self.stream()])
        self.assertEqual(self.action_outputs()["status"], "partial")
        self.assertIn("no-change", (self.directory / "summary.md").read_text())

    def test_all_no_change_and_reordered_results_are_accepted(self):
        self.result["status"] = "no-change"
        for item in self.result["results"]:
            item["status"] = "no-change"
        self.result["results"].reverse()
        self.write_envelope()
        self.execute("submit", [self.stream()])
        self.assertEqual(self.action_outputs()["status"], "no-change")

    def test_structured_failure_retains_known_batch_outcomes(self):
        self.environment["OUTPUT_MODE"] = "activity"
        self.result.update(status="failed", wiki_repository=None, wiki_sha=None,
                           reason="Source evidence unavailable; no PR could be completed.")
        for item in self.result["results"]:
            item.update(status="failed", reason="Source evidence unavailable.")
        self.write_envelope()
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.execute("submit", [self.stream()])
        outputs = self.action_outputs()
        self.assertEqual(outputs["status"], "failed")
        self.assertNotIn("wiki-sha", outputs)
        self.assertEqual(json.loads(pathlib.Path(outputs["response-path"]).read_text()), self.result)
        log = self.output.getvalue()
        self.assertIn("Maintenance batch incomplete.", log)
        self.assertNotIn("outcome is unknown", log)
        summary = (self.directory / "summary.md").read_text()
        self.assertIn(self.after, summary)
        self.assertIn("## IssueLens: Team memory batch incomplete", summary)
        self.assertIn("passed the caller's structured identity and status checks", summary)
        self.assertNotIn("outcome unknown", summary)

    def test_invalid_failed_batch_keeps_unknown_outcome_diagnostics(self):
        self.environment["OUTPUT_MODE"] = "activity"
        self.result.update(status="failed", push_after=self.before)
        for item in self.result["results"]:
            item["status"] = "failed"
        self.write_envelope()
        with self.assertRaises(SystemExit):
            self.execute("submit", [self.stream()])
        log = self.output.getvalue()
        self.assertIn("Invocation failed or its outcome is unknown.", log)
        self.assertNotIn("Maintenance batch incomplete.", log)
        summary = (self.directory / "summary.md").read_text()
        self.assertIn("Invocation failed or outcome unknown", summary)
        self.assertNotIn("passed the caller's structured identity and status checks", summary)
        self.assertFalse((self.directory / "output.txt").exists())
        self.assertFalse(list(self.directory.glob("issuelens-response-*")))

    def test_unconfirmed_stream_cannot_create_a_batch_receipt(self):
        self.environment["OUTPUT_MODE"] = "activity"
        self.write_envelope()
        with self.assertRaises(SystemExit):
            self.execute("submit", [self.stream(done=False)])
        log = self.output.getvalue()
        self.assertIn("Invocation failed or its outcome is unknown.", log)
        self.assertNotIn("Maintenance batch incomplete.", log)
        self.assertIn("Invocation failed or outcome unknown", (self.directory / "summary.md").read_text())
        self.assertFalse((self.directory / "output.txt").exists())
        self.assertFalse(list(self.directory.glob("issuelens-response-*")))

    def test_summary_respects_privacy_and_escapes_partial_reasons(self):
        self.result["status"] = "partial"
        self.result["results"][1].update(
            status="needs-review", reason="PRIVATE_REASON <script>|::error::fake-endpoint-token",
        )
        self.write_envelope()
        with self.assertRaises(SystemExit):
            self.execute("submit", [self.stream()])
        self.assertNotIn("PRIVATE_REASON", (self.directory / "summary.md").read_text())
        self.environment["SUMMARY_MODE"] = "full"
        with self.assertRaises(SystemExit):
            self.execute("submit", [self.stream()])
        summary = (self.directory / "summary.md").read_text()
        self.assertIn("PRIVATE_REASON", summary)
        self.assertIn("&lt;script&gt;", summary)
        self.assertNotIn("<script>", summary)
        self.assertNotIn("fake-endpoint-token", summary)
        self.assertNotIn("::error::", summary)

    def test_fully_deferred_batch_has_no_publication_claim(self):
        self.result.update(status="needs-review", wiki_repository=None, wiki_sha=None)
        for item in self.result["results"]:
            item.update(status="needs-review", reason="Insufficient evidence.")
        self.write_envelope()
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.execute("submit", [self.stream()])
        outputs = self.action_outputs()
        self.assertEqual(outputs["status"], "needs-review")
        self.assertNotIn("wiki-sha", outputs)
        self.assertEqual(json.loads(pathlib.Path(outputs["response-path"]).read_text()), self.result)

    def test_incomplete_batches_require_both_wiki_identity_keys(self):
        self.write_envelope()
        for status in ("needs-review", "failed"):
            for missing in (("wiki_repository",), ("wiki_sha",), ("wiki_repository", "wiki_sha")):
                with self.subTest(status=status, missing=missing):
                    result = copy.deepcopy(self.result)
                    result.update(status=status, wiki_repository=None, wiki_sha=None,
                                  reason="No PR could be completed.")
                    for item in result["results"]:
                        item.update(status=status, reason="Insufficient evidence.")
                    for field in missing:
                        del result[field]
                    with self.assertRaisesRegex(SystemExit, "requires both wiki identity fields"):
                        self.execute("submit", [self.stream(result=result)])
                    self.assertFalse((self.directory / "output.txt").exists())
                    self.assertFalse(list(self.directory.glob("issuelens-response-*")))

    def test_batch_result_cannot_omit_duplicate_or_substitute_prs(self):
        original = copy.deepcopy(self.result)
        for mutation in ("missing", "duplicate", "extra", "sha", "bool", "status", "overall",
                         "before", "after", "wiki", "reason", "padded_reason", "padded_overall_reason"):
            self.result = copy.deepcopy(original)
            if mutation == "missing":
                self.result["results"].pop()
            elif mutation == "duplicate":
                self.result["results"][1] = copy.deepcopy(self.result["results"][0])
            elif mutation == "extra":
                self.result["results"].append({**self.result["results"][0], "pull_number": 29})
            elif mutation == "sha":
                self.result["results"][0]["merge_commit_sha"] = self.after
            elif mutation == "bool":
                self.result["results"][0]["pull_number"] = True
            elif mutation == "status":
                self.result["results"][0]["status"] = "skipped"
            elif mutation == "overall":
                self.result["results"][0]["status"] = "failed"
            elif mutation in {"before", "after"}:
                self.result["push_" + mutation] = "f" * 40
            elif mutation == "wiki":
                self.result["wiki_sha"] = None
            elif mutation == "reason":
                self.result["results"][0]["reason"] = "x" * 513
            elif mutation == "padded_reason":
                self.result["results"][0]["reason"] = "x" + " " * 512
            else:
                self.result["reason"] = "x" + " " * 4096
            self.write_envelope()
            with self.subTest(mutation=mutation), self.assertRaises(SystemExit):
                self.execute("submit", [self.stream()])
            self.assertFalse((self.directory / "output.txt").exists())
            self.assertFalse(list(self.directory.glob("issuelens-response-*")))

    def test_maximum_batch_summary_stays_bounded(self):
        self.result["status"] = "partial"
        self.envelope["metadata"]["pull_requests"] = [
            {"pull_number": number, "merge_commit_sha": f"{number:040x}"}
            for number in range(1, action.MAX_BATCH_PRS + 1)
        ]
        self.result["results"] = [
            {**item, "status": "updated" if item["pull_number"] == 1 else "needs-review",
             "reason": "<&|\U0001f600" * 128}
            for item in self.envelope["metadata"]["pull_requests"]
        ]
        self.environment["SUMMARY_MODE"] = "full"
        self.write_envelope()
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.execute("submit", [self.stream()])
        summary = (self.directory / "summary.md").read_text()
        self.assertLessEqual(len(summary.encode("utf-8")), action.StreamRenderer.MAX_SUMMARY)
        self.assertIn("| 100 |", summary)
        receipt = json.loads(pathlib.Path(self.action_outputs()["response-path"]).read_text())
        self.assertEqual(len(receipt["results"]), action.MAX_BATCH_PRS)


if __name__ == "__main__":
    unittest.main()
