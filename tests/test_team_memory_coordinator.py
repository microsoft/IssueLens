import copy
import json
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
        self.execute("prepare-dispatch", self.responses(self.project))
        path = pathlib.Path(self.action_outputs()["source-event-path"])
        snapshot = json.loads(path.read_bytes())
        self.assertEqual(snapshot, self.snapshot)
        self.assertNotIn("UNTRUSTED", path.read_text())
        self.assertNotIn("fake-repository-token", path.read_text())
        self.assertEqual(path.name, "source-event.json")
        self.assertEqual(self.opener.open.call_count, 1)
        self.token.assert_not_called()

    def test_dispatch_is_one_post_to_the_fixed_default_branch_coordinator(self):
        self.select_dispatcher()
        self.write_source()
        ack = Response(b"")
        ack.status = 204
        self.execute("dispatch", self.responses(self.project) + [ack])
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/microsoft/IssueLens/actions/workflows/team-memory-coordinator.yml/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "ref": "main", "inputs": {
                "source_run_id": "123456", "source_run_attempt": "2", "source_artifact_id": "456",
            },
        })
        self.assertNotIn(b"input", request.data.replace(b'"inputs"', b''))
        self.assertNotIn(b"wiki", request.data)
        self.assertNotIn(b"fake-repository-token", request.data)
        self.token.assert_not_called()
        self.assertIn("completion is reported by the coordinator", self.output.getvalue())

    def test_dispatch_failure_never_retries_or_claims_maintenance_completion(self):
        self.select_dispatcher()
        self.write_source()
        with self.assertRaisesRegex(SystemExit, "outcome is unknown"):
            self.execute("dispatch", self.responses(self.project) + [OSError("PRIVATE TRANSPORT DETAIL")])
        self.assertEqual(self.opener.open.call_count, 2)
        self.token.assert_not_called()
        self.assertNotIn("completed", self.output.getvalue())
        self.assertFalse((self.directory / "output.txt").exists())

    def test_dispatch_uses_separate_source_read_and_coordinator_write_tokens(self):
        self.select_dispatcher()
        self.environment["DISPATCH_TOKEN"] = "fake-dispatch-token"
        path = self.write_source()
        ack = Response(b"")
        ack.status = 204
        self.execute("dispatch", self.responses(self.project) + [ack])
        read, write = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual(read.get_method(), "GET")
        self.assertEqual(read.get_header("Authorization"), "Bearer fake-repository-token")
        self.assertEqual(write.get_method(), "POST")
        self.assertEqual(write.get_header("Authorization"), "Bearer fake-dispatch-token")
        for token in ("fake-repository-token", "fake-dispatch-token"):
            self.assertNotIn(token, path.read_text())
            self.assertNotIn(token, self.output.getvalue())
            self.assertNotIn(token.encode(), write.data)
        self.token.assert_not_called()

    def test_explicit_empty_dispatch_token_never_falls_back_to_source_token(self):
        self.select_dispatcher()
        self.write_source()
        for value in ("", " \t"):
            with self.subTest(value=value):
                self.environment["DISPATCH_TOKEN"] = value
                with self.assertRaisesRegex(SystemExit, "dispatch-token must be non-empty"):
                    self.execute("dispatch", [])
                self.opener.open.assert_not_called()
                self.token.assert_not_called()

    def test_dispatcher_rejects_other_repositories_workflows_and_unsafe_pushes(self):
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
                    self.execute("prepare-dispatch", self.responses(self.project))
                self.assertFalse((self.directory / "output.txt").exists())
                self.assertFalse((self.directory / "issuelens-team-memory-source").exists())
                self.token.assert_not_called()

    def test_maximum_commit_inventory_fits_the_identity_artifact_budget(self):
        self.select_dispatcher()
        commits = [{"id": f"{index:040x}"} for index in range(1, action.MAX_PUSH_COMMITS)]
        self.event["commits"] = commits + [{"id": self.after}]
        self.execute("prepare-dispatch", self.responses(self.project))
        content = pathlib.Path(self.action_outputs()["source-event-path"]).read_bytes()
        self.assertLess(len(content), action.MAX_SOURCE_BYTES)
        self.assertEqual(len(json.loads(content)["event"]["commits"]), action.MAX_PUSH_COMMITS)

    def test_verifies_run_attempt_and_artifact_before_download(self):
        self.execute("validate-dispatch", self.source_responses())
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
                    self.execute("validate-dispatch", [])
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
                    self.execute("validate-dispatch", self.source_responses())
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
                    self.execute("validate-dispatch", self.source_responses())
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
        self.execute("validate-dispatch", self.responses(self.project))
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
        self.execute("validate-dispatch", self.source_responses())
        (self.directory / "output.txt").unlink()
        self.artifact["expired"] = True
        with self.assertRaises(SystemExit):
            self.execute("preflight", self.source_responses())
        self.assertFalse((self.directory / "output.txt").exists())
        self.token.assert_not_called()


class TeamMemoryGenericDispatchTests(unittest.TestCase):
    execute = memory_tests.TeamMemoryActionTests.execute
    action_outputs = memory_tests.TeamMemoryActionTests.action_outputs
    responses = TeamMemoryCoordinatorTests.responses
    write_source = TeamMemoryCoordinatorTests.write_source

    def setUp(self):
        TeamMemoryCoordinatorTests.setUp(self)
        self.repository = "microsoft/vscode-gradle"
        self.project.update(full_name=self.repository, default_branch="develop")
        self.source_metadata.update(
            repository=self.repository, base_ref="develop",
            workflow_ref=self.repository + "/" + action.DISPATCH_WORKFLOW + "@refs/heads/develop",
        )
        self.push.update(repository={"id": 100, "full_name": self.repository}, ref="refs/heads/develop")
        TeamMemoryCoordinatorTests.select_dispatcher(self)
        self.environment.update(
            GITHUB_REPOSITORY=self.repository, GITHUB_REF="refs/heads/develop",
            COORDINATOR_REPOSITORY="microsoft/vscode-java-pack",
            COORDINATOR_WORKFLOW="team-memory-coordinator.yml", COORDINATOR_REF="main",
            DISPATCH_TOKEN="fake-dispatch-token",
        )
        self.target_project = {"id": 200, "full_name": "microsoft/vscode-java-pack", "default_branch": "main"}
        self.target_workflow = {"id": 300, "path": action.COORDINATOR_WORKFLOW, "state": "active"}
        self.target_branch = {"name": "main", "commit": {"sha": self.tip}}

    def target_responses(self):
        return self.responses(self.target_project, self.target_workflow, self.target_branch)

    def acknowledgement(self, status=204):
        response = Response(b"")
        response.status = status
        return response

    def test_generic_artifact_preserves_exact_consumer_schema_and_full_push(self):
        self.event["commits"][0]["message"] = "UNTRUSTED_COMMIT_TEXT"
        self.event["repository"]["description"] = "UNTRUSTED_DESCRIPTION"
        self.execute("prepare-dispatch", self.responses(self.project))
        path = pathlib.Path(self.action_outputs()["source-event-path"])
        self.assertEqual(path, self.directory / "issuelens-team-memory-source" / "source-event.json")
        self.assertEqual(json.loads(path.read_bytes()), self.snapshot)
        self.assertLessEqual(len(path.read_bytes()), 64 * 1024)
        self.assertEqual([item["id"] for item in self.snapshot["event"]["commits"]], [self.merge_sha, self.after])
        for excluded in ("UNTRUSTED", "fake-", "coordinator", "source_repository"):
            self.assertNotIn(excluded, path.read_text())
        self.opener.open.assert_called_once()
        self.token.assert_not_called()

    def test_cross_repo_dispatch_authenticates_target_and_keeps_develop_separate_from_main(self):
        path = self.write_source()
        with patch.object(action, "github_request", wraps=action.github_request) as requests:
            self.execute("dispatch", self.responses(self.project) + self.target_responses() + [self.acknowledgement()])
        calls = self.opener.open.call_args_list
        self.assertEqual([call.args[0].full_url for call in calls], [
            "https://api.github.com/repos/microsoft/vscode-gradle",
            "https://api.github.com/repos/microsoft/vscode-java-pack",
            "https://api.github.com/repos/microsoft/vscode-java-pack/actions/workflows/team-memory-coordinator.yml",
            "https://api.github.com/repos/microsoft/vscode-java-pack/branches/main",
            "https://api.github.com/repos/microsoft/vscode-java-pack/actions/workflows/team-memory-coordinator.yml/dispatches",
        ])
        self.assertEqual([call.kwargs["token"] for call in requests.call_args_list],
                         [None] + [self.environment["DISPATCH_TOKEN"]] * 4)
        self.assertEqual([call.args[0].get_method() for call in calls], ["GET"] * 4 + ["POST"])
        self.assertTrue(all(call.kwargs["timeout"] == 30 for call in calls))
        request = calls[-1].args[0]
        self.assertEqual(json.loads(request.data), {
            "ref": "main", "inputs": {
                "source_repository": self.repository, "source_run_id": "123456",
                "source_run_attempt": "2", "source_artifact_id": "456",
            },
        })
        self.assertIsNone(self.builder.call_args.args[0].redirect_request(None, None, None, None, None, None))
        for token in (self.environment["GH_TOKEN"], self.environment["DISPATCH_TOKEN"]):
            self.assertNotIn(token.encode(), request.data)
            self.assertNotIn(token, path.read_text() + self.output.getvalue())
        self.token.assert_not_called()

    def test_generic_commit_inventory_exact_limit_and_artifact_budget(self):
        self.event["commits"] = [{"id": f"{index:040x}"} for index in range(1, action.MAX_PUSH_COMMITS)]
        self.event["commits"].append({"id": self.after})
        self.execute("prepare-dispatch", self.responses(self.project))
        path = pathlib.Path(self.action_outputs()["source-event-path"])
        content = path.read_bytes()
        self.assertLessEqual(len(content), 64 * 1024)
        self.assertEqual(len(json.loads(content)["event"]["commits"]), 1000)
        (self.directory / "output.txt").unlink()
        path.unlink()
        self.event["commits"].append({"id": f"{action.MAX_PUSH_COMMITS:040x}"})
        with self.assertRaisesRegex(SystemExit, "inventory"):
            self.execute("prepare-dispatch", self.responses(self.project))
        self.assertFalse(path.exists())
        self.assertFalse((self.directory / "output.txt").exists())
        self.token.assert_not_called()

    def test_explicit_same_repository_target_still_uses_generic_four_input_contract(self):
        self.environment["COORDINATOR_REPOSITORY"] = self.repository
        self.target_project = self.project
        self.environment["COORDINATOR_REF"] = "develop"
        self.target_branch["name"] = "develop"
        self.write_source()
        self.execute("dispatch", self.responses(self.project) + self.target_responses() + [self.acknowledgement()])
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(payload["ref"], "develop")
        self.assertEqual(payload["inputs"]["source_repository"], self.repository)

    def test_target_branch_path_is_url_encoded_and_not_used_as_source_branch(self):
        self.environment["COORDINATOR_REF"] = "release/1.0"
        self.target_branch["name"] = "release/1.0"
        self.write_source()
        self.execute("dispatch", self.responses(self.project) + self.target_responses() + [self.acknowledgement()])
        self.assertTrue(self.opener.open.call_args_list[-2].args[0].full_url.endswith("/branches/release%2F1.0"))
        self.assertEqual(json.loads(self.opener.open.call_args.args[0].data)["ref"], "release/1.0")

    def test_invalid_and_partial_targets_fail_before_reads_upload_or_dispatch(self):
        original = self.environment.copy()
        invalid = {
            "COORDINATOR_REPOSITORY": ("", "https://github.com/a/b", "a/b/c", "../repo", "a/b?x", "a/b\n", "-a/b"),
            "COORDINATOR_WORKFLOW": ("", ".github/workflows/team.yml", "../team.yml", "team.yml/dispatches",
                                     "team.json", "team.yml?x", "team.yml\n", "team;echo.yml"),
            "COORDINATOR_REF": ("", "refs/heads/main", "../main", "main..next", "main.lock", "main\n",
                                "main?x", "main@{0}", "main;echo", "/main", "main//next", "main/", "x" * 256),
        }
        for name, values in invalid.items():
            for value in values:
                for command in ("prepare-dispatch", "dispatch"):
                    with self.subTest(name=name, value=value, command=command):
                        self.environment = {**original, name: value}
                        with self.assertRaises(SystemExit):
                            self.execute(command, [])
                        self.opener.open.assert_not_called()
                        self.token.assert_not_called()
                        self.assertFalse((self.directory / "output.txt").exists())
                        self.assertFalse((self.directory / "issuelens-team-memory-source").exists())

    def test_target_authentication_and_identity_failures_stop_before_post(self):
        self.write_source()
        cases = [
            ([{**self.target_project, "full_name": "other/repo"}], 2),
            ([{**self.target_project, "id": True}], 2),
            ([self.target_project, {**self.target_workflow, "path": ".github/workflows/other.yml"}], 3),
            ([self.target_project, {**self.target_workflow, "state": "disabled_manually"}], 3),
            ([self.target_project, self.target_workflow, {**self.target_branch, "name": "develop"}], 4),
            ([self.target_project, self.target_workflow, {"name": "main", "commit": {"sha": "short"}}], 4),
        ]
        for values, count in cases:
            with self.subTest(values=values):
                with self.assertRaises(SystemExit):
                    self.execute("dispatch", self.responses(self.project, *values))
                self.assertEqual(self.opener.open.call_count, count)
                self.assertTrue(all(call.args[0].get_method() == "GET" for call in self.opener.open.call_args_list))
        for status in (302, 403, 404, 500):
            with self.subTest(status=status):
                failure = urllib.error.HTTPError("https://api.github.com", status, "PRIVATE DETAIL", {}, None)
                with self.assertRaises(SystemExit) as raised:
                    self.execute("dispatch", self.responses(self.project) + [failure])
                self.assertNotIn("PRIVATE DETAIL", str(raised.exception))
                self.assertEqual(self.opener.open.call_count, 2)
                self.token.assert_not_called()

    def test_dispatch_revalidates_source_snapshot_before_target_reads_or_writes(self):
        original_snapshot, original_environment = copy.deepcopy(self.snapshot), self.environment.copy()
        for change in ("workflow_sha", "workflow_path", "branch", "repository", "metadata", "body",
                       "created", "deleted", "forced", "duplicate", "truncated", "inventory_limit", "oversized"):
            with self.subTest(change=change):
                self.snapshot, self.environment = copy.deepcopy(original_snapshot), original_environment.copy()
                if change == "workflow_sha":
                    self.environment["GITHUB_WORKFLOW_SHA"] = self.tip
                elif change == "workflow_path":
                    self.environment["GITHUB_WORKFLOW_REF"] = self.repository + "/.github/workflows/other.yml@refs/heads/develop"
                elif change == "branch":
                    self.environment["GITHUB_REF"] = "refs/heads/main"
                elif change == "repository":
                    self.snapshot["event"]["repository"]["id"] = 101
                elif change == "metadata":
                    self.snapshot["metadata"]["run_attempt"] = True
                elif change == "body":
                    self.snapshot["event"]["commits"][0]["message"] = "UNTRUSTED"
                elif change in ("created", "deleted", "forced"):
                    self.snapshot["event"][change] = True
                elif change == "duplicate":
                    self.snapshot["event"]["commits"].append({"id": self.after})
                elif change == "truncated":
                    self.snapshot["event"]["commits"] = []
                elif change == "inventory_limit":
                    self.snapshot["event"]["commits"] *= action.MAX_PUSH_COMMITS
                self.write_source(b"x" * (action.MAX_SOURCE_BYTES + 1) if change == "oversized" else None)
                with self.assertRaises(SystemExit):
                    self.execute("dispatch", self.responses(self.project))
                self.assertLessEqual(self.opener.open.call_count, 1)
                self.assertTrue(all(call.args[0].get_method() == "GET" for call in self.opener.open.call_args_list))
                self.token.assert_not_called()

    def test_empty_tokens_and_invalid_artifact_ids_never_dispatch(self):
        original = self.environment.copy()
        self.write_source()
        for changes in ({"GH_TOKEN": ""}, {"DISPATCH_TOKEN": ""}, {"DISPATCH_TOKEN": " \t"},
                        {"SOURCE_ARTIFACT_ID": "0"}, {"SOURCE_ARTIFACT_ID": "1;echo"}, {"SOURCE_ARTIFACT_ID": "01"}):
            with self.subTest(changes=changes):
                self.environment = {**original, **changes}
                with self.assertRaises(SystemExit):
                    self.execute("dispatch", self.responses(self.project))
                self.assertLessEqual(self.opener.open.call_count, 1)
                self.token.assert_not_called()

    def test_unacknowledged_or_unknown_generic_dispatch_is_not_retried(self):
        self.write_source()
        for response in (self.acknowledgement(202), self.acknowledgement(302), OSError("PRIVATE DETAIL"),
                         urllib.error.HTTPError("https://api.github.com", 403, "PRIVATE DETAIL", {}, None)):
            with self.subTest(response=response):
                with self.assertRaises(SystemExit) as raised:
                    self.execute("dispatch", self.responses(self.project) + self.target_responses() + [response])
                self.assertNotIn("PRIVATE DETAIL", str(raised.exception))
                self.assertEqual(self.opener.open.call_count, 5)
                self.assertEqual(sum(call.args[0].get_method() == "POST"
                                     for call in self.opener.open.call_args_list), 1)
                self.assertNotIn("accepted", self.output.getvalue())
                self.token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
