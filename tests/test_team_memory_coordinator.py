import copy
import json
import os
import pathlib
import unittest
import urllib.error
from unittest.mock import patch

import test_team_memory_workflow as memory_tests


action = memory_tests.action
Response = memory_tests.Response


class TeamMemoryCoordinatorTests(unittest.TestCase):
    execute = memory_tests.TeamMemoryActionTests.execute
    action_outputs = memory_tests.TeamMemoryActionTests.action_outputs
    prepared_envelope = memory_tests.TeamMemoryActionTests.prepared_envelope
    write_envelope = memory_tests.TeamMemoryActionTests.write_envelope
    stream = memory_tests.TeamMemoryActionTests.stream

    def setUp(self):
        memory_tests.TeamMemoryActionTests.setUp(self)
        self.repository = "microsoft/IssueLens"
        self.project.update(full_name=self.repository, visibility="public")
        self.before, self.after, self.tip = "d" * 40, "e" * 40, "f" * 40
        self.environment.update(
            GITHUB_REPOSITORY=self.repository, GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_SHA=self.tip, GITHUB_RUN_ID="999",
            GITHUB_WORKFLOW_REF=self.repository + "/" + action.COORDINATOR_WORKFLOW + "@refs/heads/main",
            SOURCE_REPOSITORY=self.repository, SOURCE_REPOSITORIES="{}",
            SOURCE_GH_TOKEN="fake-source-token", SOURCE_RUN_ID="123456", SOURCE_RUN_ATTEMPT="2",
            PUSH_BEFORE=self.before, PUSH_AFTER=self.after, DISPATCH_PR="",
        )
        self.event = {"repository": self.project}
        self.source_project = self.project
        self.source_run = {
            "id": 123456, "run_attempt": 2, "event": "push", "path": action.DISPATCH_WORKFLOW,
            "head_branch": "main", "head_sha": self.after,
            "repository": self.project, "head_repository": self.project,
            "actor": {"login": "maintainer"}, "triggering_actor": {"login": "rerunner"},
        }
        self.branch = {"name": "main", "commit": {"sha": self.tip}}
        self.comparison = {
            "base_commit": {"sha": self.before}, "merge_base_commit": {"sha": self.before},
            "status": "ahead", "ahead_by": 2, "behind_by": 0, "total_commits": 2,
            "commits": [{"sha": self.merge_sha}, {"sha": self.after}],
        }
        self.ancestry = {
            "base_commit": {"sha": self.after}, "merge_base_commit": {"sha": self.after},
            "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
        }
        node = {
            "number": 27, "state": "MERGED", "merged": True, "mergedAt": self.pull["merged_at"],
            "baseRefName": "main", "baseRepository": {"databaseId": 100, "nameWithOwner": self.repository},
            "mergeCommit": {"oid": self.merge_sha},
        }
        connection = {"totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [node]}
        self.associations = {"data": {"repository": {
            "databaseId": 100, "nameWithOwner": self.repository,
            "defaultBranchRef": {"name": "main", "target": {"oid": self.tip}},
            "c0": {"oid": self.merge_sha, "associatedPullRequests": connection},
            "c1": {"oid": self.after, "associatedPullRequests": connection},
        }}}
        self.result.update(source_repository=self.repository, wiki_repository=self.repository)

    def responses(self, *values):
        return [Response(json.dumps(value).encode()) for value in values]

    def source_responses(self):
        values = [self.project]
        if self.source_project is not self.project:
            values.append(self.source_project)
        return self.responses(*values, self.source_run, self.branch, self.ancestry)

    def preflight_responses(self):
        return self.source_responses() + self.responses(self.comparison, self.associations)

    def select_cross_repository(self):
        self.source_project = {"id": 200, "full_name": "example/gradle", "default_branch": "develop", "visibility": "public"}
        self.environment.update(SOURCE_REPOSITORY="example/gradle", SOURCE_REPOSITORIES='{"example/gradle":200}')
        self.source_run.update(repository=self.source_project, head_repository=self.source_project, head_branch="develop")
        self.branch["name"] = "develop"
        target = self.associations["data"]["repository"]
        target.update(databaseId=200, nameWithOwner="example/gradle")
        target["defaultBranchRef"]["name"] = "develop"
        for name in ("c0", "c1"):
            node = target[name]["associatedPullRequests"]["nodes"][0]
            node.update(baseRefName="develop", baseRepository={"databaseId": 200, "nameWithOwner": "example/gradle"})

    def test_queued_range_preserves_source_identity_not_coordinator_head(self):
        self.execute("preflight", self.preflight_responses())
        envelope = self.prepared_envelope()
        metadata = envelope["metadata"]
        self.assertEqual(metadata["event_name"], "push")
        self.assertEqual(metadata["push_before"], self.before)
        self.assertEqual(metadata["push_after"], self.after)
        self.assertEqual(metadata["workflow_sha"], self.after)
        self.assertEqual(metadata["source_tip_sha"], self.tip)
        self.assertEqual(metadata["run_id"], 123456)
        self.assertEqual(metadata["coordinator_run_id"], 999)
        self.assertEqual(metadata["required_wiki_repository"], self.repository)
        self.assertEqual(metadata["pull_requests"][0]["merge_commit_sha"], self.merge_sha)
        self.assertEqual(len(metadata["pull_requests"]), 1)
        self.assertEqual(metadata["range_origin"], "authorized-reconciliation")
        self.assertIn("not an attestation of the original push boundary", envelope["request"]["input"])
        self.assertIn("Preflight already verified the complete fast-forward commit inventory", envelope["request"]["input"])
        self.assertIn("Do not repeat that range validation with compare_commits", envelope["request"]["input"])
        self.assertIn("list_pull_request_files(per_page=1, page=1)", envelope["request"]["input"])
        self.assertIn("get_file(ref=push_after)", envelope["request"]["input"])
        self.assertNotIn("fake-source-token", json.dumps(envelope))
        self.assertNotIn("fake-repository-token", json.dumps(envelope))
        self.assertFalse((self.directory / "issuelens-team-memory-source").exists())
        calls = self.opener.open.call_args_list
        self.assertEqual(calls[0].args[0].get_header("Authorization"), "Bearer fake-repository-token")
        for call in calls[1:]:
            self.assertEqual(call.args[0].get_header("Authorization"), "Bearer fake-source-token")
            self.assertEqual(call.kwargs["timeout"], 30)
        self.assertIn("/attempts/2", calls[1].args[0].full_url)
        self.assertIn(f"/compare/{self.before}...{self.after}?per_page=100&page=1", calls[4].args[0].full_url)
        self.token.assert_not_called()

    def test_cross_repository_source_develop_is_independent_of_coordinator_main(self):
        self.select_cross_repository()
        self.execute("preflight", self.preflight_responses())
        metadata = self.prepared_envelope()["metadata"]
        self.assertEqual(metadata["repository"], "example/gradle")
        self.assertEqual(metadata["repository_id"], 200)
        self.assertEqual(metadata["base_ref"], "develop")
        self.assertEqual(metadata["workflow_ref"], "example/gradle/" + action.DISPATCH_WORKFLOW + "@refs/heads/develop")
        self.assertEqual(metadata["coordinator_repository"], self.repository)
        self.assertEqual(metadata["required_wiki_repository"], self.repository)
        for call in self.opener.open.call_args_list[1:]:
            request = call.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer fake-source-token")
            if request.full_url.endswith("/graphql"):
                self.assertEqual(json.loads(request.data)["variables"], {"owner": "example", "name": "gradle"})
            else:
                self.assertIn("/repos/example/gradle/", request.full_url + "/")

    def test_public_cross_repository_reads_accept_caller_token_or_explicit_override(self):
        self.select_cross_repository()
        self.pull["base"] = {"ref": "develop", "repo": self.source_project}
        for name in ("c0", "c1"):
            self.associations["data"]["repository"][name]["associatedPullRequests"]["nodes"][0]["mergeCommit"] = None
        for source_token in (self.environment["GH_TOKEN"], "fake-source-token"):
            with self.subTest(source_token=source_token):
                self.environment["SOURCE_GH_TOKEN"] = source_token
                self.execute("preflight", self.preflight_responses() + self.responses(self.pull))
                envelope = self.prepared_envelope()
                self.assertEqual(self.action_outputs()["eligible"], "true")
                self.assertEqual(envelope["metadata"]["repository"], self.source_project["full_name"])
                self.assertEqual(envelope["metadata"]["source_tip_sha"], self.tip)
                self.assertEqual(envelope["metadata"]["pull_requests"][0]["merge_commit_sha"], self.merge_sha)
                requests = [call.args[0] for call in self.opener.open.call_args_list]
                self.assertEqual([request.full_url for request in requests], [
                    f"https://api.github.com/repos/{self.repository}",
                    "https://api.github.com/repos/example/gradle",
                    "https://api.github.com/repos/example/gradle/actions/runs/123456/attempts/2",
                    "https://api.github.com/repos/example/gradle/branches/develop",
                    f"https://api.github.com/repos/example/gradle/compare/{self.after}...{self.tip}?per_page=1&page=2",
                    f"https://api.github.com/repos/example/gradle/compare/{self.before}...{self.after}?per_page=100&page=1",
                    "https://api.github.com/graphql",
                    "https://api.github.com/repos/example/gradle/pulls/27",
                ])
                self.assertEqual(requests[0].get_header("Authorization"), "Bearer " + self.environment["GH_TOKEN"])
                self.assertTrue(all(request.get_header("Authorization") == "Bearer " + source_token
                                    for request in requests[1:]))
                self.assertEqual(json.loads(requests[-2].data)["variables"], {"owner": "example", "name": "gradle"})
                for credential in (self.environment["GH_TOKEN"], source_token):
                    self.assertNotIn(credential, json.dumps(envelope))
                    self.assertNotIn(credential, self.output.getvalue())
                self.token.assert_not_called()

    def test_same_repository_is_default_and_unchanged_tip_needs_no_ancestry_read(self):
        self.environment["SOURCE_REPOSITORY"] = ""
        self.environment["SOURCE_GH_TOKEN"] = self.environment["GH_TOKEN"]
        self.branch["commit"]["sha"] = self.after
        self.associations["data"]["repository"]["defaultBranchRef"]["target"]["oid"] = self.after
        self.execute("preflight", self.responses(self.project, self.source_run, self.branch, self.comparison, self.associations))
        self.assertEqual(self.prepared_envelope()["metadata"]["source_tip_sha"], self.after)
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertTrue(all(call.args[0].get_header("Authorization") == "Bearer " + self.environment["GH_TOKEN"]
                            for call in self.opener.open.call_args_list))

    def test_rebase_rest_identity_lookup_keeps_the_source_token(self):
        for name in ("c0", "c1"):
            self.associations["data"]["repository"][name]["associatedPullRequests"]["nodes"][0]["mergeCommit"] = None
        self.execute("preflight", self.preflight_responses() + self.responses(self.pull))
        metadata = self.prepared_envelope()["metadata"]
        self.assertEqual(metadata["pull_requests"][0]["merge_commit_sha"], self.merge_sha)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.github.com/repos/{self.repository}/pulls/27")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-source-token")
        self.assertEqual(sum(call.args[0].full_url.endswith("/pulls/27")
                             for call in self.opener.open.call_args_list), 1)

    def test_invalid_or_mixed_source_inputs_fail_before_network(self):
        original = self.environment.copy()
        cases = [
            {"SOURCE_RUN_ID": ""}, {"SOURCE_RUN_ATTEMPT": ""}, {"PUSH_BEFORE": ""}, {"PUSH_AFTER": ""},
            {"SOURCE_RUN_ID": "1; echo unsafe"}, {"SOURCE_RUN_ATTEMPT": "0"}, {"PUSH_BEFORE": "0" * 40},
            {"PUSH_AFTER": "short"}, {"PUSH_AFTER": self.before}, {"DISPATCH_PR": "27"},
            {"SOURCE_REPOSITORY": "example/private"},
            {"SOURCE_REPOSITORY": "example/.."}, {"SOURCE_REPOSITORY": "example/../secret"},
            {"SOURCE_REPOSITORY": "example/\nsecret"}, {"SOURCE_REPOSITORY": "-example/project"},
            {"SOURCE_REPOSITORIES": " " * 4097},
            {"SOURCE_REPOSITORIES": json.dumps({f"example/p{index}": index + 1 for index in range(101)})},
            {"SOURCE_REPOSITORIES": '{"example/gradle":true}'}, {"SOURCE_REPOSITORIES": "[]"},
            {"SOURCE_REPOSITORIES": '{"example/gradle":200,"example/gradle":200}'},
            {"SOURCE_REPOSITORIES": '{"example/gradle":200,"EXAMPLE/gradle":200}'},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.environment = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("preflight", [])
                self.opener.open.assert_not_called()
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_missing_or_invalid_source_token_does_not_fall_back_to_caller_token(self):
        self.select_cross_repository()
        for source_token in (None, "", " ", "fake-source-token\nPRIVATE", "token\x01private", "t" * 4097):
            with self.subTest(source_token=source_token):
                if source_token is None:
                    self.environment.pop("SOURCE_GH_TOKEN", None)
                else:
                    self.environment["SOURCE_GH_TOKEN"] = source_token
                with self.assertRaisesRegex(SystemExit, "source-github-token must be non-empty printable ASCII"):
                    self.execute("preflight", [])
                self.opener.open.assert_not_called()
                self.token.assert_not_called()
                self.assertFalse((self.directory / "output.txt").exists())
                self.assertEqual(list(self.directory.glob("issuelens-request-*.json")), [])

    def test_public_source_auth_failure_never_falls_back_or_retries(self):
        self.select_cross_repository()
        for source_token in (self.environment["GH_TOKEN"], "fake-source-token"):
            for status in (401, 403):
                for offset in (1, 2, 5, 6):
                    with self.subTest(source_token=source_token, status=status, offset=offset):
                        self.environment["SOURCE_GH_TOKEN"] = source_token
                        failure = urllib.error.HTTPError(
                            "https://api.github.com", status, source_token + " PRIVATE API DETAIL", {}, None)
                        with self.assertRaises(SystemExit) as raised:
                            self.execute("preflight", self.preflight_responses()[:offset] + [failure])
                        self.assertEqual(self.opener.open.call_count, offset + 1)
                        self.assertTrue(all(call.args[0].get_header("Authorization") == "Bearer " + source_token
                                            for call in self.opener.open.call_args_list[1:]))
                        self.assertNotIn(source_token, str(raised.exception))
                        self.assertNotIn("PRIVATE", str(raised.exception))
                        self.assertFalse((self.directory / "output.txt").exists())
                        self.assertEqual(list(self.directory.glob("issuelens-request-*.json")), [])
                        self.token.assert_not_called()

    def test_coordinator_inputs_are_not_available_to_other_adapters_or_workflows(self):
        original = self.environment.copy()
        for changes in (
            {"REQUEST_TYPE": "issue-loop"}, {"REQUEST_TYPE": "task", "TASK_INPUT": "Do something"},
            {"GITHUB_WORKFLOW_REF": self.repository + "/" + action.DISPATCH_WORKFLOW + "@refs/heads/main"},
            {"GITHUB_REF": "refs/heads/feature"},
        ):
            with self.subTest(changes=changes):
                self.environment = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.responses(self.project))
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_forged_source_runs_fail_before_discovery_or_login(self):
        self.select_cross_repository()
        self.environment["SOURCE_GH_TOKEN"] = self.environment["GH_TOKEN"]
        original = copy.deepcopy(self.source_run)
        cases = [
            {"id": 123457}, {"run_attempt": 1}, {"event": "pull_request"},
            {"path": ".github/workflows/untrusted.yml"}, {"head_branch": "untrusted"},
            {"head_sha": self.tip}, {"repository": {**self.source_project, "id": 201}},
            {"repository": {**self.source_project, "full_name": "example/renamed"}},
            {"head_repository": {**self.source_project, "id": 201}}, {"actor": {"login": "unsafe\nactor"}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.source_run = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.source_responses())
                self.assertEqual(self.opener.open.call_count, 3)
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_cross_repository_policy_identity_and_visibility_fail_closed(self):
        self.select_cross_repository()
        self.environment["SOURCE_GH_TOKEN"] = self.environment["GH_TOKEN"]
        original = self.source_project.copy()
        for changes in ({"id": 201}, {"full_name": "example/renamed"}, {"visibility": "private"}, {"visibility": "internal"}):
            with self.subTest(changes=changes):
                self.source_project.update(original)
                self.source_project.update(changes)
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.preflight_responses())
                self.assertEqual(self.opener.open.call_count, 2)
                self.token.assert_not_called()
        self.source_project.update(original)
        for visibility in ("private", "internal"):
            with self.subTest(coordinator_visibility=visibility):
                self.project["visibility"] = visibility
                with self.assertRaisesRegex(SystemExit, "public"):
                    self.execute("preflight", self.preflight_responses())
                self.assertEqual(self.opener.open.call_count, 2)
                self.token.assert_not_called()

    def test_branch_ancestry_and_ref_races_fail_before_agent_login(self):
        self.select_cross_repository()
        self.environment["SOURCE_GH_TOKEN"] = self.environment["GH_TOKEN"]
        originals = copy.deepcopy((self.branch, self.ancestry, self.associations))
        for mutation in ("branch", "sha", "diverged", "merge_base", "ref_race"):
            with self.subTest(mutation=mutation):
                self.branch, self.ancestry, self.associations = copy.deepcopy(originals)
                if mutation == "branch":
                    self.branch["name"] = "feature"
                elif mutation == "sha":
                    self.branch["commit"]["sha"] = "short"
                elif mutation == "diverged":
                    self.ancestry["behind_by"] = 1
                elif mutation == "merge_base":
                    self.ancestry["merge_base_commit"]["sha"] = self.before
                else:
                    self.associations["data"]["repository"]["defaultBranchRef"]["target"]["oid"] = "1" * 40
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.preflight_responses())
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_range_rejects_truncated_duplicate_divergent_or_missing_head_inventory(self):
        original = copy.deepcopy(self.comparison)
        for changes in (
            {"commits": [{"sha": self.after}]},
            {"commits": [{"sha": self.after}, {"sha": self.after}]},
            {"commits": [{"sha": self.merge_sha}, {"sha": self.before}]},
            {"commits": [{"sha": self.merge_sha}, {"sha": self.tip}]},
            {"commits": [{"sha": self.merge_sha}, {"sha": "short"}]},
            {"status": "diverged"}, {"ahead_by": 3}, {"total_commits": 1001},
            {"total_commits": True}, {"total_commits": 0},
            {"merge_base_commit": {"sha": self.tip}},
        ):
            with self.subTest(changes=changes):
                self.comparison = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.preflight_responses())
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_complete_comparison_pagination_covers_1000_and_requires_every_page(self):
        shas = [f"{index:040x}" for index in range(1, 1000)] + [self.after]
        pages = [{**self.comparison, "ahead_by": 1000, "total_commits": 1000,
                  "commits": [{"sha": sha} for sha in shas[start:start + 100]]}
                 for start in range(0, 1000, 100)]
        for source_token in (self.environment["GH_TOKEN"], "fake-source-token"):
            with self.subTest(source_token=source_token):
                self.environment["SOURCE_GH_TOKEN"] = source_token
                with patch.dict(os.environ, self.environment, clear=True), \
                        patch.object(action, "github_read", side_effect=pages) as read:
                    result = action.read_reconciliation_inventory(self.repository, self.before, self.after, float("inf"))
                self.assertEqual(result, shas)
                self.assertEqual(read.call_count, 10)
                self.assertEqual([call.args[0] for call in read.call_args_list], [
                    f"/repos/{self.repository}/compare/{self.before}...{self.after}?per_page=100&page={page}"
                    for page in range(1, 11)
                ])
                self.assertTrue(all(call.kwargs["token"] == source_token for call in read.call_args_list))
        for mutation in ("missing", "duplicate", "count", "base"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(pages)
                if mutation == "missing":
                    changed[1]["commits"].pop()
                elif mutation == "duplicate":
                    changed[1]["commits"][0] = changed[0]["commits"][0]
                elif mutation == "count":
                    changed[1]["total_commits"] = 999
                else:
                    changed[1]["base_commit"]["sha"] = self.tip
                with patch.dict(os.environ, self.environment, clear=True), \
                        patch.object(action, "github_read", side_effect=changed), self.assertRaises(ValueError):
                    action.read_reconciliation_inventory(self.repository, self.before, self.after, float("inf"))

    def test_partial_final_page_and_discovery_deadline(self):
        shas = [f"{index:040x}" for index in range(1, 102)] + [self.after]
        pages = [{**self.comparison, "ahead_by": len(shas), "total_commits": len(shas),
                  "commits": [{"sha": sha} for sha in shas[start:start + 100]]}
                 for start in range(0, len(shas), 100)]
        with patch.dict(os.environ, self.environment, clear=True), patch.object(action, "github_read", side_effect=pages):
            self.assertEqual(action.read_reconciliation_inventory(self.repository, self.before, self.after, float("inf")), shas)
        with patch.dict(os.environ, self.environment, clear=True), patch.object(action, "github_read") as read, \
                self.assertRaisesRegex(ValueError, "time budget"):
            action.read_reconciliation_inventory(self.repository, self.before, self.after, 0)
        read.assert_not_called()

    def test_network_errors_do_not_retry_or_expose_credentials(self):
        responses = self.source_responses() + [OSError("fake-source-token PRIVATE API DETAIL")]
        with self.assertRaises(SystemExit) as raised:
            self.execute("preflight", responses)
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertNotIn("fake-source-token", str(raised.exception))
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertFalse((self.directory / "output.txt").exists())
        self.token.assert_not_called()

    def test_valid_range_without_newly_merged_prs_skips_before_azure_login(self):
        for name in ("c0", "c1"):
            self.associations["data"]["repository"][name]["associatedPullRequests"] = {
                "totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": [],
            }
        self.execute("preflight", self.preflight_responses())
        self.assertEqual(self.action_outputs()["eligible"], "false")
        self.assertEqual(self.action_outputs()["skip-reason"], "no_merged_pull_requests")
        self.token.assert_not_called()

    def test_queued_request_preserves_the_existing_batch_receipt_contract(self):
        self.execute("preflight", self.preflight_responses())
        prepared = self.prepared_envelope()
        self.environment["REQUEST_PATH"] = self.action_outputs()["request-path"]
        (self.directory / "output.txt").unlink()
        self.result = {
            "status": "updated", "source_repository": self.repository,
            "push_before": self.before, "push_after": self.after,
            "wiki_repository": self.repository.lower(), "wiki_sha": "c" * 40,
            "reason": "Tool-confirmed publication.", "results": [{
                "pull_number": 27, "merge_commit_sha": self.merge_sha,
                "status": "updated", "reason": "Verified and published.",
            }],
        }
        self.execute("submit", [self.stream()])
        outputs = self.action_outputs()
        self.assertEqual(outputs["status"], "updated")
        self.assertEqual(json.loads(pathlib.Path(outputs["response-path"]).read_text()), self.result)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), prepared["request"])
        self.assertEqual(self.opener.open.call_count, 1)
        self.token.assert_called_once()

    def test_manual_pr_selection_uses_the_same_coordinator_without_range_inputs(self):
        self.environment.update({name: "" for name in action.SOURCE_INPUTS}, DISPATCH_PR="27")
        self.execute("preflight", self.responses(self.project, self.pull))
        metadata = self.prepared_envelope()["metadata"]
        self.assertEqual(metadata["pull_number"], 27)
        self.assertEqual(metadata["required_wiki_repository"], self.repository)
        self.assertEqual(metadata["coordinator_run_id"], 999)
        self.assertEqual(self.opener.open.call_count, 2)
        self.token.assert_not_called()

    def test_manual_cross_source_requires_allowlist_and_uses_source_read_credentials(self):
        self.select_cross_repository()
        self.environment.update({name: "" for name in action.SOURCE_INPUTS}, DISPATCH_PR="27")
        self.pull["base"] = {"ref": "develop", "repo": self.source_project}
        self.execute("preflight", self.responses(self.project, self.source_project, self.pull))
        metadata = self.prepared_envelope()["metadata"]
        self.assertEqual(metadata["repository"], "example/gradle")
        self.assertEqual(metadata["base_ref"], "develop")
        self.assertEqual(metadata["coordinator_repository"], self.repository)
        self.assertEqual(self.opener.open.call_args.args[0].get_header("Authorization"), "Bearer fake-source-token")

    def test_coordinator_wiki_identity_is_required_without_overriding_policy(self):
        self.envelope["metadata"].update(repository=self.repository, required_wiki_repository=self.repository)
        self.write_envelope()
        with self.assertRaisesRegex(SystemExit, "wiki destination"):
            self.execute("submit", [self.stream({**self.result, "wiki_repository": "microsoft/vscode-java-pack"})])
        self.assertFalse((self.directory / "output.txt").exists())
        self.assertIn("never overrides repository policy", action.build_team_memory_request(self.envelope["metadata"])["input"])

    def test_obsolete_artifact_and_dispatch_entrypoints_are_removed(self):
        for command in ("dispatch", "prepare-dispatch", "validate-dispatch", "prepare-source", "validate-source"):
            with self.subTest(command=command), self.assertRaisesRegex(SystemExit, "Unsupported action command"):
                self.execute(command, [])
            self.opener.open.assert_not_called()
        for name in ("prepare_source", "validate_source", "source_event_path", "read_source_event", "team_memory_source_snapshot"):
            self.assertFalse(hasattr(action, name))

    def test_other_direct_callers_can_still_use_the_same_workflow_filename(self):
        self.environment.update({name: "" for name in (*action.SOURCE_INPUTS, "SOURCE_REPOSITORY")}, DISPATCH_PR="27")
        self.environment["GITHUB_REPOSITORY"] = "example/project"
        self.environment["GITHUB_WORKFLOW_REF"] = "example/project/" + action.COORDINATOR_WORKFLOW + "@refs/heads/main"
        self.project["full_name"] = "example/project"
        self.event = {"repository": self.project}
        self.execute("preflight", self.responses(self.project, self.pull))
        self.assertNotIn("required_wiki_repository", self.prepared_envelope()["metadata"])


if __name__ == "__main__":
    unittest.main()
