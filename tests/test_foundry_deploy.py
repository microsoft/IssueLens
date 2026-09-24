import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest.mock import patch


ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("foundry_deploy", ROOT / ".github/scripts/foundry_deploy.py")
assert SPEC is not None and SPEC.loader is not None
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
SHA = "a" * 40
PROJECT = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/agents/providers/Microsoft.CognitiveServices/accounts/foundry/projects/production"
ENDPOINT = "https://foundry.services.ai.azure.com/api/projects/production"


def config():
    return {
        "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
        "AZURE_TENANT_ID": "33333333-3333-3333-3333-333333333333",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_LOCATION": "eastus", "AZURE_AI_PROJECT_ID": PROJECT,
        "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT, "AZURE_AI_MODEL_DEPLOYMENT_NAME": "triage-model",
        "AZURE_AI_MODEL_API_KEY": "", "GITHUB_APP_ID": "12345",
        "GITHUB_APP_PRIVATE_KEY_SECRET_URI": "https://vault.vault.azure.net/secrets/issuelens-app",
        "TOOLBOX_ENDPOINT": "", "MAILING_URL": "", "PERSONAL_NOTIFICATION_URL": "",
    }


def sse(event, name=None):
    prefix = f"event: {name}\n" if name else ""
    return (prefix + "data: " + json.dumps(event) + "\n\n").encode()


def invocation_body(text=deploy.ACKNOWLEDGEMENT):
    return sse({"type": "assistant.message", "data": {"content": text, "phase": "final"}}) + sse(
        {"invocation_id": "invocation-1", "session_id": "session-1"}, "done",
    )


def completion(text=deploy.ACKNOWLEDGEMENT):
    return {"type": "response.completed", "response": {
        "id": "response-1", "status": "completed", "error": None, "incomplete_details": None,
        "output": [{"type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text}]}],
    }}


def raw(body):
    return b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n" + body


class FoundryDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = pathlib.Path(temporary.name)
        self.root = self.directory / "repository"
        self.runner = self.directory / "runner"
        self.root.mkdir()
        self.runner.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(deploy, "ROOT", self.root).start()
        patch.dict(os.environ, {
            "RUNNER_TEMP": str(self.runner), "GITHUB_SHA": SHA,
            "GITHUB_REPOSITORY": "microsoft/IssueLens", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main", "GITHUB_STEP_SUMMARY": str(self.runner / "summary.md"),
        }).start()

    def github_fixtures(self):
        run = {
            "id": 77, "run_attempt": 2, "path": ".github/workflows/ci.yml",
            "head_sha": SHA, "head_branch": "main", "event": "push",
            "status": "completed", "conclusion": "success",
            "head_repository": {"full_name": "microsoft/IssueLens"},
        }
        return {
            "": {"default_branch": "main"},
            "compare": {"status": "ahead", "merge_base_commit": {"sha": SHA}},
            "environment": {
                "name": "foundry-production", "can_admins_bypass": False,
                "protection_rules": [{"type": "required_reviewers", "prevent_self_review": True,
                                      "reviewers": [{"type": "Team", "reviewer": {"id": 42}}]}],
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            },
            "policies": {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]},
            "runs": {"workflow_runs": [run]},
            "jobs": {"total_count": len(deploy.CI_JOBS), "jobs": [
                {"name": name, "status": "completed", "conclusion": "success", "head_sha": SHA}
                for name in sorted(deploy.CI_JOBS)
            ]},
        }

    def run_preflight(self, fixtures):
        def read(path, label):
            if path == "":
                key = ""
            elif path.startswith("/compare/"):
                self.assertIn(SHA, path)
                key = "compare"
            elif "deployment-branch-policies" in path:
                key = "policies"
            elif path.startswith("/environments/"):
                key = "environment"
            elif path.startswith("/actions/workflows/ci.yml/runs?"):
                self.assertIn("event=push", path)
                self.assertIn(f"head_sha={SHA}", path)
                self.assertIn("per_page=1", path)
                key = "runs"
            else:
                self.assertEqual(path, "/actions/runs/77/attempts/2/jobs?per_page=100")
                key = "jobs"
            return fixtures[key]
        with patch.object(deploy, "github", side_effect=read), \
                patch.object(deploy, "cli", return_value=(SHA + "\n").encode()), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.preflight()

    def test_preflight_checks_latest_exact_commit_and_complete_job_inventory(self):
        self.run_preflight(self.github_fixtures())

    def test_preflight_rejects_missing_or_bypassable_human_protections(self):
        base = self.github_fixtures()
        cases = [
            ("environment", "can_admins_bypass", True),
            ("environment", "can_admins_bypass", None),
            ("environment", "protection_rules", []),
            ("environment", "protection_rules", [{"type": "required_reviewers", "reviewers": [42]}]),
            ("environment", "deployment_branch_policy", None),
            ("policies", "branch_policies", [{"name": "*", "type": "branch"}]),
            ("policies", "branch_policies", [{"name": "main", "type": "tag"}]),
            ("policies", "total_count", 2),
            ("compare", "status", "diverged"),
            ("compare", "merge_base_commit", {"sha": "b" * 40}),
        ]
        for section, key, value in cases:
            with self.subTest(section=section, key=key, value=value):
                fixture = copy.deepcopy(base)
                fixture[section][key] = value
                with self.assertRaises(ValueError):
                    self.run_preflight(fixture)

    def test_preflight_rejects_wrong_trigger_branch_and_repository(self):
        for key, value in (
            ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_REF", "refs/tags/main"),
            ("GITHUB_EVENT_NAME", "push"), ("GITHUB_REPOSITORY", "someone/IssueLens"),
        ):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    self.run_preflight(self.github_fixtures())

    def test_malformed_github_fields_are_controlled_failures(self):
        for section, key, value in (
            ("compare", "merge_base_commit", None), ("environment", "protection_rules", None),
            ("environment", "protection_rules", ["unexpected"]), ("policies", "branch_policies", None),
            ("runs", "workflow_runs", [None]), ("runs", "workflow_runs", None),
            ("jobs", "jobs", None), ("jobs", "jobs", ["unexpected"] * len(deploy.CI_JOBS)),
        ):
            with self.subTest(section=section, key=key):
                fixtures = self.github_fixtures()
                fixtures[section][key] = value
                with self.assertRaises(ValueError):
                    self.run_preflight(fixtures)
        with patch.object(deploy._action, "github_read", return_value=[]):
            with self.assertRaisesRegex(ValueError, "invalid object"):
                deploy.github("", "Read repository")

    def test_preflight_does_not_accept_stale_failed_or_spoofed_ci(self):
        for key, value in (
            ("status", "in_progress"), ("conclusion", "failure"), ("conclusion", "cancelled"),
            ("conclusion", "skipped"), ("head_sha", "b" * 40), ("event", "pull_request"),
            ("head_branch", "feature"), ("path", ".github/workflows/another.yml"),
            ("head_repository", {"full_name": "fork/IssueLens"}),
        ):
            with self.subTest(key=key, value=value):
                fixture = self.github_fixtures()
                fixture["runs"]["workflow_runs"][0][key] = value
                with self.assertRaises(ValueError):
                    self.run_preflight(fixture)
        fixture = self.github_fixtures()
        fixture["runs"]["workflow_runs"] = []
        with self.assertRaisesRegex(ValueError, "No valid CI run"):
            self.run_preflight(fixture)

    def test_ci_job_inventory_must_not_be_truncated_or_skipped(self):
        for change in ("truncated", "skipped", "other-sha", "duplicate"):
            with self.subTest(change=change):
                fixture = self.github_fixtures()
                jobs = fixture["jobs"]
                if change == "truncated":
                    jobs["total_count"] += 1
                elif change == "skipped":
                    jobs["jobs"][0]["conclusion"] = "skipped"
                elif change == "other-sha":
                    jobs["jobs"][0]["head_sha"] = "b" * 40
                else:
                    jobs["jobs"][0]["name"] = jobs["jobs"][1]["name"]
                with self.assertRaisesRegex(ValueError, "Required CI jobs"):
                    self.run_preflight(fixture)

    def test_github_failure_never_exposes_response_body(self):
        error = urllib.error.HTTPError("https://api.github.com", 403, "private response", {}, None)
        with patch.object(deploy._action, "github_read", side_effect=error):
            with self.assertRaisesRegex(ValueError, "GitHub HTTP 403") as caught:
                deploy.github("/environments/foundry-production", "Environment")
        self.assertNotIn("private response", str(caught.exception))

    def test_configuration_preserves_optional_managed_identity_and_notifications(self):
        self.assertEqual(deploy.configuration(config()), config())
        values = config()
        values.update(AZURE_AI_MODEL_API_KEY="model-key-fixture",
                      TOOLBOX_ENDPOINT=ENDPOINT + "/toolboxes/notifications/versions/1/mcp?api-version=v1",
                      MAILING_URL="https://eastus.logic.azure.com/workflows/mail?sig=fixture",
                      PERSONAL_NOTIFICATION_URL="https://eastus.logic.azure.com/workflows/teams?sig=fixture")
        self.assertEqual(deploy.configuration(values), values)

    def test_invalid_configuration_is_rejected_without_echoing_values(self):
        cases = (
            ("AZURE_CLIENT_ID", "invalid"), ("AZURE_TENANT_ID", ""),
            ("AZURE_SUBSCRIPTION_ID", "44444444-4444-4444-4444-444444444444"),
            ("AZURE_AI_PROJECT_ID", PROJECT + "/agents/IssueLens"),
            ("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT + "/another"),
            ("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT + "?sig=private-value"),
            ("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT.replace(".services.ai.azure.com", ".attacker.test")),
            ("AZURE_LOCATION", "East US"), ("AZURE_AI_MODEL_DEPLOYMENT_NAME", ""),
            ("GITHUB_APP_ID", "0"), ("GITHUB_APP_PRIVATE_KEY_SECRET_URI", "private-key-fixture"),
            ("GITHUB_APP_PRIVATE_KEY_SECRET_URI", "https://vault.vault.azure.net/secrets/key?sig=private-value"),
            ("TOOLBOX_ENDPOINT", "https://elsewhere.services.ai.azure.com/api/projects/other/toolboxes/a"),
            ("MAILING_URL", "http://eastus.logic.azure.com"),
            ("MAILING_URL", "https://example.test:private-value/"),
            ("PERSONAL_NOTIFICATION_URL", "https://user:private-value@example.test"),
            ("AZURE_AI_MODEL_API_KEY", "private-value\nNEXT=bad"),
        )
        for key, value in cases:
            with self.subTest(key=key):
                values = config()
                values[key] = value
                with self.assertRaises(ValueError) as caught:
                    deploy.configuration(values)
                self.assertNotIn("private-value", str(caught.exception))
                self.assertNotIn("private-key-fixture", str(caught.exception))

    def prepare(self, values=None):
        with patch.dict(os.environ, values or config()), \
                patch.object(deploy, "cli", return_value=(SHA + "\n").encode()), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.prepare()

    def test_prepare_keeps_secrets_out_of_the_receipt_and_log(self):
        values = {**config(), "AZURE_AI_MODEL_API_KEY": "runtime-secret-fixture"}
        self.prepare(values)
        receipt = deploy.scratch("issuelens-deployment.json").read_text()
        self.assertNotIn("runtime-secret-fixture", receipt)
        self.assertIn("runtime-secret-fixture", deploy.scratch("issuelens-deployment-config.json").read_text())
        self.assertEqual(deploy.read_state()["publication"], "not-started")
        with self.assertRaises(FileExistsError):
            self.prepare(values)

    def configure_results(self):
        return [
            {"id": config()["AZURE_SUBSCRIPTION_ID"], "tenantId": config()["AZURE_TENANT_ID"]},
            {"id": PROJECT, "location": "eastus", "properties": {"endpoints": {"AI Foundry API": ENDPOINT + "/"}}},
            {"name": "IssueLens"},
            config(),
        ]

    def test_configure_verifies_existing_target_before_loading_azd(self):
        self.prepare()
        calls = []

        def run(arguments, label, **kwargs):
            calls.append(arguments)
            if arguments[:3] == ["azd", "env", "set"]:
                path = pathlib.Path(arguments[arguments.index("--file") + 1])
                content = path.read_text()
                self.assertIn('AZURE_AI_MODEL_API_KEY=""', content)
                self.assertIn(f'FOUNDRY_PROJECT_ENDPOINT="{ENDPOINT}"', content)
            return b""

        with patch.object(deploy, "cli_json", side_effect=self.configure_results()) as reads, \
                patch.object(deploy, "cli", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            deploy.configure()
        self.assertEqual(reads.call_count, 4)
        self.assertEqual(calls[0], ["azd", "config", "set", "auth.useAzCliAuth", "true"])
        self.assertEqual(calls[1][:4], ["azd", "env", "new", "foundry-production"])
        self.assertFalse(deploy.scratch("issuelens-deployment-config.json").exists())
        self.assertTrue(deploy.read_state()["owns_azure_state"])
        for call in calls:
            self.assertFalse(set(call) & {"provision", "up", "assignment", "keyvault"})

    def test_target_mismatches_stop_before_azd_configuration(self):
        self.prepare()
        for index, key, value in (
            (0, "tenantId", "another-tenant"), (0, "id", "another-subscription"),
            (0, "id", None), (0, "tenantId", None),
            (1, "location", "westus"), (1, "id", PROJECT + "-wrong"),
            (1, "properties", {"endpoints": {}}), (1, "properties", None), (2, "name", "AnotherAgent"),
        ):
            with self.subTest(index=index, key=key):
                results = self.configure_results()
                results[index][key] = value
                with patch.object(deploy, "cli_json", side_effect=results), patch.object(deploy, "cli") as writes:
                    with self.assertRaises(ValueError):
                        deploy.configure()
                    writes.assert_not_called()

    def test_dotenv_escapes_secret_interpolation_and_verifies_the_round_trip(self):
        values = {**config(), "AZURE_AI_MODEL_API_KEY": 'fixture-${AZURE_LOCATION}-"$KEY"'}
        self.prepare(values)
        results = self.configure_results()
        results[-1] = values

        def run(arguments, label, **kwargs):
            if arguments[:3] == ["azd", "env", "set"]:
                path = pathlib.Path(arguments[arguments.index("--file") + 1])
                self.assertIn(r'AZURE_AI_MODEL_API_KEY="fixture-\${AZURE_LOCATION}-\"\$KEY\""', path.read_text())
            return b""

        with patch.object(deploy, "cli_json", side_effect=results), patch.object(deploy, "cli", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.configure()
        self.prepare(values)
        with patch.object(deploy, "cli_json", side_effect=self.configure_results()), \
                patch.object(deploy, "cli", return_value=b""):
            with self.assertRaisesRegex(ValueError, "did not preserve"):
                deploy.configure()

    def write_sources(self):
        sources = {
            "main.py": b"print('fixture')\n", "requirements.txt": b"fixture-package\n",
            "agents/issuelens.md": b"Fixture prompt\n",
            "skills/team-memory/SKILL.md": b"Fixture skill\n",
            "schemas/report.json": b"{}\n",
            "github_app_mcp/src/issuelens_github_mcp/server.py": b"pass\n",
        }
        for name, content in sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return sources

    def archive(self, sources, additions=None):
        path = self.runner / "fixture.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in sources.items():
                archive.writestr(name, content)
            for name, content in additions or []:
                if isinstance(name, str):
                    entry = zipfile.ZipInfo()
                    entry.filename = name
                else:
                    entry = name
                archive.writestr(entry, content)
        return path

    def test_package_inspection_covers_runtime_bytes_and_secret_exclusions(self):
        sources = self.write_sources()
        path = self.archive(sources)
        digest = deploy.inspect_package(path, set(sources))
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        for name in (
            ".env", ".env.production", ".azure/prod/.env", ".git", ".git/config",
            ".github/secret.txt", ".foundry/state", ".venv/lib/module.py",
            ".issuelens-copilot/session", "nested/app.pem", "nested/app.pfx",
            "../outside.py", "/absolute.py", "C:/absolute.py", "nested\\outside.py",
        ):
            with self.subTest(name=name):
                path = self.archive(sources, [(name, b"unwanted")])
                with self.assertRaises(ValueError):
                    deploy.inspect_package(path, set(sources))
        path = self.archive(sources, [("notes.txt", b"runtime-secret-fixture")])
        with self.assertRaisesRegex(ValueError, "configured runtime secret"):
            deploy.inspect_package(path, set(sources), ["runtime-secret-fixture"])

    def test_missing_modified_duplicate_or_symlinked_package_entries_fail(self):
        sources = self.write_sources()
        missing = {key: value for key, value in sources.items() if key != "main.py"}
        with self.assertRaisesRegex(ValueError, "missing"):
            deploy.inspect_package(self.archive(missing), set(sources))
        changed = {**sources, "main.py": b"different\n"}
        with self.assertRaisesRegex(ValueError, "approved checkout"):
            deploy.inspect_package(self.archive(changed), set(sources))
        with self.assertWarns(UserWarning):
            path = self.archive(sources, [("main.py", sources["main.py"])])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            deploy.inspect_package(path, set(sources))
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        path = self.archive(sources, [(symlink, b"main.py")])
        with self.assertRaisesRegex(ValueError, "unsafe"):
            deploy.inspect_package(path, set(sources))

    def test_package_limits_are_measured(self):
        sources = self.write_sources()
        path = self.archive(sources)
        with patch.object(deploy, "MAX_PACKAGE", path.stat().st_size - 1):
            with self.assertRaisesRegex(ValueError, "oversized"):
                deploy.inspect_package(path, set(sources))

    def test_postpackage_inspects_exactly_one_isolated_native_zip(self):
        sources = self.write_sources()
        directory = deploy.scratch("issuelens-code-package")
        directory.mkdir()
        path = self.archive(sources)
        path.rename(directory / "azd-code-deploy-fixture.zip")
        with patch.dict(os.environ, {"ISSUELENS_PACKAGE_CHECK_DIR": str(directory)}), \
                patch.object(deploy, "checked_sha", return_value=SHA), patch.object(deploy, "cli", return_value=b""), \
                patch.object(deploy, "required_runtime_files", return_value=set(sources)), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.package_check()
            self.assertRegex(deploy.read_state()["package_sha256"], r"^[0-9a-f]{64}$")
            (directory / "azd-code-deploy-extra.zip").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                deploy.package_check()

    def test_publish_calls_native_deploy_once_and_requires_package_receipt(self):
        def publish(arguments, label, timeout, env):
            self.assertEqual(arguments, [
                "azd", "deploy", "IssueLens", "--environment", "foundry-production", "--no-prompt", "--timeout", "1200",
            ])
            self.assertEqual(timeout, 1260)
            self.assertEqual(env["TMPDIR"], str(deploy.scratch("issuelens-code-package")))
            self.assertEqual(env["ISSUELENS_PACKAGE_CHECK_DIR"], env["TMPDIR"])
            self.assertEqual(deploy.read_state()["publication"], "attempted")
            deploy.record(package_sha256="c" * 64)
            return b""
        with patch.object(deploy, "checked_sha"), patch.object(deploy, "cli", side_effect=publish) as cli, \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.publish()
        cli.assert_called_once()
        self.assertEqual(deploy.read_state()["publication"], "completed")

    def test_missing_hook_receipt_cannot_claim_success_or_retry(self):
        with patch.object(deploy, "checked_sha"), patch.object(deploy, "cli", return_value=b"") as cli:
            with self.assertRaisesRegex(ValueError, "package-inspection receipt"):
                deploy.publish()
        cli.assert_called_once()
        self.assertEqual(deploy.read_state()["publication"], "attempted")

    def readiness(self, status="active"):
        return {
            "name": "IssueLens", "version": "7", "status": status,
            "agent_endpoints": {
                "invocations": ENDPOINT + "/agents/IssueLens/endpoint/protocols/invocations?api-version=v1",
                "responses": ENDPOINT + "/agents/IssueLens/endpoint/protocols/openai/responses?api-version=v1",
            },
        }

    def test_verify_binds_both_nonempty_requests_to_the_published_version(self):
        deploy.record(publication="completed", target=ENDPOINT)
        calls = []

        def run(arguments, label, **kwargs):
            calls.append(arguments)
            if arguments[:3] == ["azd", "env", "get-value"]:
                return b"7\n"
            self.assertEqual(arguments[arguments.index("--version") + 1], "7")
            self.assertIn("--new-session", arguments)
            self.assertEqual(arguments[arguments.index("--timeout") + 1], "120")
            self.assertEqual(kwargs["timeout"], 150)
            body = pathlib.Path(arguments[arguments.index("--input-file") + 1]).read_text()
            protocol = arguments[arguments.index("--protocol") + 1]
            if protocol == "responses":
                self.assertEqual(body, deploy.SMOKE_INPUT)
                self.assertIn("--new-conversation", arguments)
            else:
                self.assertEqual(json.loads(body), {"input": deploy.SMOKE_INPUT})
                self.assertNotIn("--new-conversation", arguments)
            return raw(invocation_body() if protocol == "invocations" else sse(completion()))

        with patch.object(deploy, "cli", side_effect=run), \
                patch.object(deploy, "cli_json", return_value=self.readiness()), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.verify()
        self.assertEqual(len(calls), 3)
        self.assertEqual(deploy.read_state()["invocations"], "passed")
        self.assertEqual(deploy.read_state()["responses"], "passed")
        self.assertEqual(deploy.read_state()["version"], "7")

    def test_readiness_must_match_version_and_target(self):
        deploy.record(publication="completed", target=ENDPOINT)
        for key, value in (
            ("status", "failed"), ("name", "other"), ("version", "8"),
            ("agent_endpoints", None),
            ("agent_endpoints", {"invocations": "https://attacker.test/protocols/invocations"}),
        ):
            with self.subTest(key=key):
                readiness = {**self.readiness(), key: value}
                with patch.object(deploy, "cli", return_value=b"7\n") as cli, \
                        patch.object(deploy, "cli_json", return_value=readiness):
                    with self.assertRaises(ValueError):
                        deploy.verify()
                    cli.assert_called_once()

    def test_readiness_wait_is_bounded_without_deployment_retries(self):
        deploy.record(publication="completed", target=ENDPOINT)
        with patch.object(deploy, "cli", return_value=b"7\n") as cli, \
                patch.object(deploy, "cli_json", return_value=self.readiness("pending")), \
                patch.object(deploy.time, "monotonic", side_effect=[0, 181]):
            with self.assertRaisesRegex(ValueError, "deadline"):
                deploy.verify()
        cli.assert_called_once()

    def test_http_status_content_type_and_headers_are_not_success_proxies(self):
        for data in (
            b"hello", raw(invocation_body()).replace(b"200 OK", b"500 Error"),
            raw(invocation_body()).replace(b"text/event-stream", b"application/json"),
            b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * (64 * 1024) + b"\r\n\r\n",
        ):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                deploy.http_response(data)

    def test_invocation_errors_truncation_and_tool_calls_are_rejected(self):
        for body in (
            sse({"type": "error", "message": "private error"}) + invocation_body(),
            sse({"type": "session.error"}) + invocation_body(),
            sse({"type": "tool.execution_start", "data": {"toolName": "write_wiki_pages"}}) + invocation_body(),
            sse({"type": "assistant.message", "data": {"toolRequests": [{"name": "send-email"}]}}) + invocation_body(),
            sse({"type": "assistant.message", "data": {"content": deploy.ACKNOWLEDGEMENT}}),
        ):
            with self.subTest(body=body[:80]):
                response, unused = deploy.http_response(raw(body))
                with self.assertRaises(ValueError):
                    deploy._action.read_response(response, deploy.NoTools())

    def test_responses_requires_complete_successful_message_not_just_http_completion(self):
        event = completion()
        self.assertEqual(deploy.responses_text(sse(event)), deploy.ACKNOWLEDGEMENT)
        broken = [
            sse({"type": "error"}) + sse(event), sse({"type": "response.failed"}) + sse(event),
            sse({"type": "message"}, "error") + sse(event),
            sse({"type": "response.completed", "response": []}),
            sse(event) + sse(event), sse(event).rstrip(),
            sse({"type": "response.completed", "response": {"status": "failed"}}),
            sse({"type": "response.completed", "response": {**event["response"], "output": "invalid"}}),
        ]
        for change in ("error", "incomplete_details"):
            modified = copy.deepcopy(event)
            modified["response"][change] = {"message": "private error"}
            broken.append(sse(modified))
        modified = copy.deepcopy(event)
        modified["response"]["output"][0]["status"] = "in_progress"
        broken.append(sse(modified))
        modified = copy.deepcopy(event)
        modified["response"]["output"] = [{"type": "function_call", "name": "send-email"}]
        broken.append(sse(modified))
        for body in broken:
            with self.subTest(body=body[:90]), self.assertRaises(ValueError):
                deploy.responses_text(body)

    def test_completed_invocation_with_trailing_error_does_not_pass(self):
        deploy.record(publication="completed", target=ENDPOINT)
        body = invocation_body() + sse({"type": "session.error"})
        with patch.object(deploy, "cli", side_effect=[b"7\n", raw(body)]), \
                patch.object(deploy, "cli_json", return_value=self.readiness()):
            with self.assertRaisesRegex(ValueError, "events after completion"):
                deploy.verify()
        self.assertEqual(deploy.read_state()["invocations"], "attempted")

    def test_response_stream_limit_and_malformed_content_fail(self):
        body = sse(completion())
        with patch.object(deploy, "MAX_OUTPUT", len(body) - 1):
            with self.assertRaisesRegex(ValueError, "stream exceeds"):
                deploy.responses_text(body)
        for content in (None, "not-a-list", [None]):
            event = completion()
            event["response"]["output"][0]["content"] = content
            with self.subTest(content=content), self.assertRaises(ValueError):
                deploy.responses_text(sse(event))

    def test_wrong_acknowledgement_fails_and_retains_publication_receipt(self):
        deploy.record(publication="completed", target=ENDPOINT)
        with patch.object(deploy, "cli", side_effect=[b"7\n", raw(invocation_body("GitHub access is not configured"))]), \
                patch.object(deploy, "cli_json", return_value=self.readiness()):
            with self.assertRaisesRegex(ValueError, "expected acknowledgement"):
                deploy.verify()
        self.assertEqual(deploy.read_state()["publication"], "completed")
        self.assertEqual(deploy.read_state()["invocations"], "attempted")
        self.assertNotIn("responses", deploy.read_state())

    def test_cli_errors_and_timeouts_withhold_private_output(self):
        def fail(*args, **kwargs):
            kwargs["stdout"].write(b"runtime-secret-fixture")
            kwargs["stderr"].write(b"secret-bearing-url")
            return subprocess.CompletedProcess(args[0], 1)
        with patch.object(deploy.subprocess, "run", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "exit code 1") as caught:
                deploy.cli(["azd", "deploy"], "Deployment")
        self.assertNotIn("runtime-secret-fixture", str(caught.exception))
        with patch.object(deploy.subprocess, "run", side_effect=subprocess.TimeoutExpired("private argument", 1)):
            with self.assertRaisesRegex(ValueError, "may still be running") as caught:
                deploy.cli(["azd", "deploy"], "Deployment")
        self.assertNotIn("private argument", str(caught.exception))

    def test_cli_output_limit_is_measured(self):
        def output(*args, **kwargs):
            kwargs["stdout"].write(b"12345")
            return subprocess.CompletedProcess(args[0], 0)
        with patch.object(deploy, "MAX_OUTPUT", 4), patch.object(deploy.subprocess, "run", side_effect=output):
            with self.assertRaisesRegex(ValueError, "output limit"):
                deploy.cli(["azd", "show"], "Readiness")

    def test_failed_publication_summary_is_not_no_write_claim(self):
        deploy.record(commit=SHA, target=ENDPOINT, publication="attempted", version="7", invocations="passed",
                      responses="attempted", error="Responses did not return the expected acknowledgement")
        with patch.dict(os.environ, {"PUBLISH_OUTCOME": "success", "VERIFY_OUTCOME": "failure"}):
            deploy.summary()
        summary = pathlib.Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
        self.assertIn("A version may already have been published", summary)
        self.assertIn("No automatic retry or rollback", summary)
        self.assertIn(SHA, summary)
        self.assertIn("Agent version:** `7`", summary)

    def test_summary_before_prepare_does_not_claim_deployment(self):
        with patch.dict(os.environ, {"PUBLISH_OUTCOME": "skipped", "VERIFY_OUTCOME": "skipped"}):
            deploy.summary()
        summary = pathlib.Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
        self.assertIn("not started", summary)
        self.assertIn("not recorded", summary)
        self.assertNotIn("A version may already", summary)

    def test_cleanup_deletes_only_owned_azd_and_named_temporary_state(self):
        azure = self.root / ".azure"
        azure.mkdir()
        (azure / "keep.env").write_text("untouched")
        deploy.cleanup()
        self.assertTrue((azure / "keep.env").exists())
        deploy.record(owns_azure_state=True)
        deploy.scratch("issuelens-deployment-config.json").write_text("private")
        package = deploy.scratch("issuelens-code-package")
        package.mkdir()
        (package / "archive.zip").write_bytes(b"fixture")
        unrelated = self.runner / "unrelated"
        unrelated.write_text("keep")
        deploy.cleanup()
        self.assertFalse(azure.exists())
        self.assertFalse(package.exists())
        self.assertTrue(unrelated.exists())

    def test_main_does_not_echo_parser_input_or_overwrite_hook_failure(self):
        deploy.record(error="Code package contains a configured runtime secret")
        with patch.object(deploy, "publish", side_effect=ValueError("Deployment failed")), \
                patch.object(sys, "argv", ["helper", "publish"]), contextlib.redirect_stdout(io.StringIO()) as log:
            with self.assertRaises(SystemExit):
                deploy.main()
        self.assertIn("Deployment failed", log.getvalue())
        self.assertEqual(deploy.read_state()["error"], "Code package contains a configured runtime secret")
        with patch.object(deploy, "prepare", side_effect=json.JSONDecodeError("private-value", "private-input", 0)), \
                patch.object(sys, "argv", ["helper", "prepare"]), contextlib.redirect_stdout(io.StringIO()) as log:
            with self.assertRaises(SystemExit):
                deploy.main()
        self.assertNotIn("private-value", log.getvalue())
        self.assertNotIn("private-input", log.getvalue())


if __name__ == "__main__":
    unittest.main()
