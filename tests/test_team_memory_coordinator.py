import copy
import json
import pathlib
import unittest

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
        self.project["full_name"] = self.repository
        self.before, self.after, self.tip = "d" * 40, "e" * 40, "f" * 40
        self.environment.update(
            GITHUB_REPOSITORY=self.repository, GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_SHA=self.tip, GITHUB_RUN_ID="999",
            GITHUB_WORKFLOW_REF=self.repository + "/" + action.COORDINATOR_WORKFLOW + "@refs/heads/main",
            SOURCE_RUN_ID="123456", SOURCE_RUN_ATTEMPT="2", SOURCE_ARTIFACT_ID="456",
            DISPATCH_PR="",
        )
        self.event = {"repository": self.project}
        self.source_run = {
            "id": 123456, "run_attempt": 2, "event": "push", "path": action.DISPATCH_WORKFLOW,
            "head_branch": "main", "head_sha": self.after,
            "repository": self.project, "head_repository": self.project,
            "actor": {"login": "maintainer"}, "triggering_actor": {"login": "rerunner"},
        }
        self.artifact = {
            "id": 456, "name": "issuelens-team-memory-source-2", "expired": False, "size_in_bytes": 2048,
            "digest": "sha256:" + "1" * 64,
            "workflow_run": {
                "id": 123456, "repository_id": 100, "head_repository_id": 100,
                "head_branch": "main", "head_sha": self.after,
            },
        }
        self.push = {
            "repository": {"id": 100, "full_name": self.repository}, "ref": "refs/heads/main",
            "before": self.before, "after": self.after, "created": False, "deleted": False, "forced": False,
            "commits": [{"id": self.merge_sha}, {"id": self.after}], "head_commit": {"id": self.after},
        }
        self.source_metadata = {
            "repository": self.repository, "repository_id": 100, "base_ref": "main",
            "event_name": "push", "event_action": "push",
            "actor_login": "maintainer", "triggering_actor": "rerunner",
            "workflow_ref": self.repository + "/" + action.DISPATCH_WORKFLOW + "@refs/heads/main",
            "workflow_sha": self.after, "run_id": 123456, "run_attempt": 2,
        }
        self.snapshot = {"metadata": self.source_metadata, "event": self.push}
        self.comparison = {
            "base_commit": {"sha": self.before}, "merge_base_commit": {"sha": self.before},
            "status": "ahead", "ahead_by": 2, "behind_by": 0, "total_commits": 2,
            "commits": [{"sha": self.after}],
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
        return self.responses(self.project, self.source_run, self.artifact)

    def write_source(self, content=None):
        path = self.directory / "issuelens-team-memory-source" / "source-event.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(json.dumps(self.snapshot).encode() if content is None else content)
        return path

    def select_dispatcher(self):
        self.environment.update(
            GITHUB_EVENT_NAME="push", GITHUB_SHA=self.after, GITHUB_WORKFLOW_SHA=self.after,
            GITHUB_WORKFLOW_REF=self.source_metadata["workflow_ref"], GITHUB_RUN_ID="123456",
        )
        self.environment.pop("SOURCE_RUN_ID")
        self.environment.pop("SOURCE_RUN_ATTEMPT")
        self.event = copy.deepcopy(self.push)

    def test_dispatcher_preserves_only_identities_and_never_invokes_foundry(self):
        self.select_dispatcher()
        self.event["commits"][0]["message"] = "UNTRUSTED_COMMIT_TEXT"
        self.event["head_commit"]["message"] = "UNTRUSTED_HEAD_TEXT"
        self.event["repository"]["description"] = "UNTRUSTED_REPOSITORY_TEXT"
        self.execute("prepare-source", self.responses(self.project))
        path = pathlib.Path(self.action_outputs()["source-event-path"])
        snapshot = json.loads(path.read_bytes())
        self.assertEqual(snapshot, self.snapshot)
        self.assertNotIn("UNTRUSTED", path.read_text())
        self.assertNotIn("fake-repository-token", path.read_text())
        self.assertEqual(path.name, "source-event.json")
        self.assertEqual(self.opener.open.call_count, 1)
        self.token.assert_not_called()

    def test_source_preparation_rejects_mismatched_repositories_workflows_and_unsafe_pushes(self):
        self.select_dispatcher()
        original_environment, original_event = self.environment.copy(), copy.deepcopy(self.event)
        for change in ("repository", "workflow", "branch", "workflow_sha", "forced", "truncated"):
            with self.subTest(change=change):
                self.environment, self.event = original_environment.copy(), copy.deepcopy(original_event)
                if change == "repository":
                    self.environment["GITHUB_REPOSITORY"] = "microsoft/vscode-java-pack"
                elif change == "workflow":
                    self.environment["GITHUB_WORKFLOW_REF"] = self.repository + "/.github/workflows/untrusted.yml@refs/heads/main"
                elif change == "branch":
                    self.environment["GITHUB_REF"] = "refs/heads/untrusted"
                elif change == "workflow_sha":
                    self.environment["GITHUB_WORKFLOW_SHA"] = "b" * 40
                elif change == "forced":
                    self.event["forced"] = True
                else:
                    self.event["commits"] = []
                with self.assertRaises(SystemExit):
                    self.execute("prepare-source", self.responses(self.project))
                self.assertFalse((self.directory / "output.txt").exists())
                self.assertFalse((self.directory / "issuelens-team-memory-source").exists())
                self.token.assert_not_called()

    def test_maximum_commit_inventory_fits_the_identity_artifact_budget(self):
        self.select_dispatcher()
        commits = [{"id": f"{index:040x}"} for index in range(1, action.MAX_PUSH_COMMITS)]
        self.event["commits"] = commits + [{"id": self.after}]
        self.execute("prepare-source", self.responses(self.project))
        content = pathlib.Path(self.action_outputs()["source-event-path"]).read_bytes()
        self.assertLess(len(content), action.MAX_SOURCE_BYTES)
        self.assertEqual(len(json.loads(content)["event"]["commits"]), action.MAX_PUSH_COMMITS)
        pathlib.Path(self.action_outputs()["source-event-path"]).unlink()
        (self.directory / "output.txt").unlink()
        self.event["commits"].append({"id": f"{action.MAX_PUSH_COMMITS:040x}"})
        with self.assertRaisesRegex(SystemExit, "inventory"):
            self.execute("prepare-source", self.responses(self.project))
        self.assertFalse((self.directory / "output.txt").exists())

    def test_request_owned_source_supports_develop_and_exact_unchanged_snapshot(self):
        self.select_dispatcher()
        repository = "example/gradle"
        self.project.update(full_name=repository, default_branch="develop")
        self.environment.update(
            GITHUB_REPOSITORY=repository, GITHUB_REF="refs/heads/develop",
            GITHUB_WORKFLOW_REF=repository + "/" + action.DISPATCH_WORKFLOW + "@refs/heads/develop",
        )
        self.event.update(repository={"id": 100, "full_name": repository}, ref="refs/heads/develop")
        self.snapshot["metadata"].update(repository=repository, base_ref="develop",
                                         workflow_ref=self.environment["GITHUB_WORKFLOW_REF"])
        self.snapshot["event"].update(repository={"id": 100, "full_name": repository}, ref="refs/heads/develop")
        self.execute("prepare-source", self.responses(self.project))
        path = pathlib.Path(self.action_outputs()["source-event-path"])
        self.assertEqual(json.loads(path.read_bytes()), self.snapshot)
        self.assertNotIn("fake-repository-token", path.read_text())
        self.assertNotIn("coordinator", path.read_text())
        self.assertEqual([call.args[0].get_method() for call in self.opener.open.call_args_list], ["GET"])
        self.token.assert_not_called()

    def test_actual_prepared_artifact_round_trips_through_coordinator_validation_and_discovery(self):
        coordinator_environment = self.environment.copy()
        self.select_dispatcher()
        self.execute("prepare-source", self.responses(self.project))
        actual = pathlib.Path(self.action_outputs()["source-event-path"]).read_bytes()
        (self.directory / "output.txt").unlink()
        self.environment = coordinator_environment
        self.event = {"repository": self.project}
        self.execute("validate-source", self.source_responses())
        (self.directory / "output.txt").unlink()
        self.execute("preflight", self.source_responses() + self.responses(self.comparison, self.associations))
        metadata = self.prepared_envelope()["metadata"]
        self.assertEqual(json.loads(actual), self.snapshot)
        self.assertEqual(metadata["push_before"], self.before)
        self.assertEqual(metadata["push_after"], self.after)
        self.assertEqual(metadata["source_tip_sha"], self.tip)
        self.assertNotEqual(metadata["workflow_sha"], self.environment["GITHUB_SHA"])
        self.assertEqual(metadata["pull_requests"][0]["merge_commit_sha"], self.merge_sha)
        self.token.assert_not_called()

    def test_obsolete_dispatch_entrypoints_are_removed(self):
        for command in ("dispatch", "prepare-dispatch", "validate-dispatch"):
            with self.subTest(command=command), self.assertRaisesRegex(SystemExit, "Unsupported action command"):
                self.execute(command, [])
            self.opener.open.assert_not_called()
        for name in ("dispatch", "prepare_dispatch", "validate_dispatch", "dispatch_target", "validate_dispatch_target"):
            self.assertFalse(hasattr(action, name))

    def test_source_download_validation_cannot_be_used_by_other_request_adapters(self):
        self.environment["REQUEST_TYPE"] = "task"
        with self.assertRaisesRegex(SystemExit, "Only team-memory"):
            self.execute("validate-source", [])
        self.opener.open.assert_not_called()

    def test_verifies_run_attempt_and_artifact_before_download(self):
        self.execute("validate-source", self.source_responses())
        self.assertEqual(self.action_outputs(), {
            "automatic": "true", "source-run-id": "123456", "source-artifact-id": "456",
        })
        self.assertEqual([call.args[0].full_url for call in self.opener.open.call_args_list], [
            "https://api.github.com/repos/microsoft/IssueLens",
            "https://api.github.com/repos/microsoft/IssueLens/actions/runs/123456/attempts/2",
            "https://api.github.com/repos/microsoft/IssueLens/actions/artifacts/456",
        ])
        self.token.assert_not_called()

    def test_invalid_or_mixed_source_inputs_fail_before_network(self):
        original = self.environment.copy()
        cases = [
            {"SOURCE_RUN_ID": ""}, {"SOURCE_RUN_ATTEMPT": ""}, {"SOURCE_ARTIFACT_ID": ""},
            {"SOURCE_RUN_ID": "1; echo unsafe"}, {"SOURCE_RUN_ATTEMPT": "0"}, {"SOURCE_ARTIFACT_ID": "01"},
            {"DISPATCH_PR": "27"}, {name: "" for name in action.SOURCE_INPUTS},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.environment = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("validate-source", [])
                self.opener.open.assert_not_called()
                self.assertFalse((self.directory / "output.txt").exists())

    def test_forged_source_runs_fail_before_artifact_download_or_login(self):
        original = copy.deepcopy(self.source_run)
        cases = [
            {"id": 123457}, {"run_attempt": 1}, {"event": "pull_request"},
            {"path": ".github/workflows/untrusted.yml"}, {"head_branch": "untrusted"},
            {"head_sha": "short"}, {"repository": {**self.project, "id": 101}},
            {"head_repository": {**self.project, "id": 101}}, {"actor": {"login": "unsafe\nactor"}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.source_run = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("validate-source", self.source_responses())
                self.assertEqual(self.opener.open.call_count, 2)
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_foreign_expired_or_oversized_artifacts_fail_closed(self):
        original = copy.deepcopy(self.artifact)
        cases = [
            {"id": 457}, {"name": "issuelens-team-memory-source-1"}, {"expired": True},
            {"digest": None}, {"digest": "short"},
            {"size_in_bytes": 0}, {"size_in_bytes": True}, {"size_in_bytes": action.MAX_SOURCE_BYTES + 1},
            {"workflow_run": {**original["workflow_run"], "id": 123457}},
            {"workflow_run": {**original["workflow_run"], "repository_id": 101}},
            {"workflow_run": {**original["workflow_run"], "head_repository_id": 101}},
            {"workflow_run": {**original["workflow_run"], "head_branch": "untrusted"}},
            {"workflow_run": {**original["workflow_run"], "head_sha": self.tip}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.artifact = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("validate-source", self.source_responses())
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_queued_push_uses_original_inventory_not_coordinator_head(self):
        self.write_source()
        self.execute("preflight", self.source_responses() + self.responses(self.comparison, self.associations))
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
        self.assertIn("before writing", envelope["request"]["input"])
        self.assertNotIn("fake-repository-token", json.dumps(envelope))
        self.token.assert_not_called()

    def test_source_snapshot_mismatch_missing_file_and_size_fail_before_discovery(self):
        original = copy.deepcopy(self.snapshot)
        for mutation in ("metadata", "head", "body", "forced", "oversized", "missing"):
            with self.subTest(mutation=mutation):
                self.snapshot = copy.deepcopy(original)
                if mutation == "metadata":
                    self.snapshot["metadata"]["run_attempt"] = 1
                elif mutation == "head":
                    self.snapshot["event"]["after"] = self.tip
                elif mutation == "body":
                    self.snapshot["event"]["commits"][0]["message"] = "UNTRUSTED"
                elif mutation == "forced":
                    self.snapshot["event"]["forced"] = True
                path = self.write_source(b"x" * (action.MAX_SOURCE_BYTES + 1) if mutation == "oversized" else None)
                if mutation == "missing":
                    path.unlink()
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.source_responses())
                self.assertEqual(self.opener.open.call_count, 3)
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_truncated_discovery_and_api_errors_do_not_submit_partial_batches(self):
        self.write_source()
        for response in ({**self.comparison, "total_commits": 3}, OSError("PRIVATE API DETAIL")):
            with self.subTest(response=response):
                responses = self.source_responses()
                responses += [response] if isinstance(response, Exception) else self.responses(response)
                with self.assertRaises(SystemExit):
                    self.execute("preflight", responses)
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_valid_push_without_newly_merged_prs_skips_before_azure_login(self):
        self.write_source()
        for name in ("c0", "c1"):
            self.associations["data"]["repository"][name]["associatedPullRequests"] = {
                "totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": [],
            }
        self.execute("preflight", self.source_responses() + self.responses(self.comparison, self.associations))
        self.assertEqual(self.action_outputs()["eligible"], "false")
        self.assertEqual(self.action_outputs()["skip-reason"], "no_merged_pull_requests")
        self.token.assert_not_called()

    def test_queued_request_preserves_the_existing_batch_receipt_contract(self):
        self.write_source()
        self.execute("preflight", self.source_responses() + self.responses(self.comparison, self.associations))
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

    def test_manual_pr_selection_uses_the_same_coordinator_without_source_artifacts(self):
        self.environment.update({name: "" for name in action.SOURCE_INPUTS}, DISPATCH_PR="27")
        self.execute("validate-source", self.responses(self.project))
        self.assertEqual(self.action_outputs(), {"automatic": "false"})
        (self.directory / "output.txt").unlink()
        self.execute("preflight", self.responses(self.project, self.pull))
        metadata = self.prepared_envelope()["metadata"]
        self.assertEqual(metadata["pull_number"], 27)
        self.assertEqual(metadata["required_wiki_repository"], self.repository)
        self.assertEqual(metadata["coordinator_run_id"], 999)
        self.assertEqual(self.opener.open.call_count, 2)
        self.token.assert_not_called()

    def test_coordinator_inputs_are_not_available_to_other_adapters_or_repositories(self):
        original = self.environment.copy()
        for changes in (
            {"REQUEST_TYPE": "issue-loop"}, {"REQUEST_TYPE": "task", "TASK_INPUT": "Do something"},
            {"GITHUB_REPOSITORY": "microsoft/vscode-java-pack"},
            {"GITHUB_WORKFLOW_REF": self.source_metadata["workflow_ref"]},
        ):
            with self.subTest(changes=changes):
                self.environment = {**original, **changes}
                if changes.get("GITHUB_REPOSITORY"):
                    self.event["repository"] = {**self.project, "full_name": changes["GITHUB_REPOSITORY"]}
                with self.assertRaises(SystemExit):
                    self.execute("preflight", self.source_responses())
                self.assertFalse((self.directory / "output.txt").exists())
                self.token.assert_not_called()

    def test_coordinator_wiki_identity_is_required_without_overriding_policy(self):
        self.envelope["metadata"].update(repository=self.repository, required_wiki_repository=self.repository)
        self.write_envelope()
        with self.assertRaisesRegex(SystemExit, "wiki destination"):
            self.execute("submit", [self.stream({**self.result, "wiki_repository": "microsoft/vscode-java-pack"})])
        self.assertFalse((self.directory / "output.txt").exists())
        self.assertIn("never overrides repository policy", action.build_team_memory_request(self.envelope["metadata"])["input"])

    def test_other_callers_can_still_use_the_same_workflow_filename(self):
        self.environment.update({name: "" for name in action.SOURCE_INPUTS}, DISPATCH_PR="27")
        self.environment["GITHUB_REPOSITORY"] = "example/project"
        self.environment["GITHUB_WORKFLOW_REF"] = "example/project/" + action.COORDINATOR_WORKFLOW + "@refs/heads/main"
        self.project["full_name"] = "example/project"
        self.event = {"repository": self.project}
        self.execute("preflight", self.responses(self.project, self.pull))
        self.assertNotIn("required_wiki_repository", self.prepared_envelope()["metadata"])

    def test_coordinator_preflight_revalidates_after_source_download(self):
        self.write_source()
        self.execute("validate-source", self.source_responses())
        (self.directory / "output.txt").unlink()
        self.artifact["expired"] = True
        with self.assertRaises(SystemExit):
            self.execute("preflight", self.source_responses())
        self.assertFalse((self.directory / "output.txt").exists())
        self.token.assert_not_called()




if __name__ == "__main__":
    unittest.main()
