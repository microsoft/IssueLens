import json
import os
import pathlib
import re
import shutil
import subprocess
import unittest

import yaml


ROOT = pathlib.Path(__file__).parents[1]
FOUNDRY_EXTENSIONS = {
    "azure.ai.agents": "1.0.0-beta.16",
    "azure.ai.connections": "1.0.0-beta.7",
    "azure.ai.inspector": "1.0.0-beta.7",
    "azure.ai.projects": "1.0.0-beta.11",
    "azure.ai.routines": "1.0.0-beta.6",
    "azure.ai.skills": "1.0.0-beta.6",
    "azure.ai.toolboxes": "1.0.0-beta.7",
    "microsoft.foundry": "1.0.0-beta.2",
}


class FoundryDeploymentWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / ".github/workflows/deploy-foundry.yml").read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.source, Loader=yaml.BaseLoader)
        cls.deploy = cls.workflow["jobs"]["deploy-and-test"]
        cls.steps = cls.deploy["steps"]
        cls.commands = "\n".join(step.get("run", "") for step in cls.steps)
        cls.gate = cls.steps[1]
        cls.extensions = next(step for step in cls.steps
                              if step["name"] == "Install and verify the Foundry extension")

    def run_workflow_step(self, step, stub, env):
        bash = shutil.which("bash")
        self.assertIsNotNone(bash, "Executable workflow tests require Bash")
        self.assertIsNotNone(shutil.which("jq"), "Executable workflow tests require jq")
        return subprocess.run(
            [str(pathlib.Path(bash).resolve()), "--noprofile", "--norc", "-eo", "pipefail",
             "-c", stub + step["run"]],
            env=os.environ | step.get("env", {}) | env,
            capture_output=True, text=True, timeout=15,
        )

    def protected_environment(self, prevent_self_review):
        return {
            "can_admins_bypass": False,
            "protection_rules": [{
                "type": "required_reviewers", "prevent_self_review": prevent_self_review,
                "reviewers": [{"type": "User", "reviewer": {"id": 1}}],
            }],
        }

    def run_github_gates(self, environment, runs, *, environment_exit=0, ci_exit=0):
        stub = """
gh() {
  printf 'GH_CALL: %s\\n' "$*" >&2
  case "$*" in
    "api repos/$GITHUB_REPOSITORY/environments/$AZD_ENV_NAME --jq "*)
      jq -rb "${@: -1}" <<<"$ENVIRONMENT"
      return "$ENVIRONMENT_EXIT" ;;
    "api --method GET repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs -f event=push -f branch=$DEFAULT_BRANCH -f head_sha=$GITHUB_SHA -F per_page=1 --jq "*)
      jq -rb "${@: -1}" <<<"$CI_RUNS"
      return "$CI_EXIT" ;;
    *) return 99 ;;
  esac
}
"""
        return self.run_workflow_step(self.gate, stub, {
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "microsoft/IssueLens",
            "AZD_ENV_NAME": "foundry-production",
            "DEFAULT_BRANCH": "main",
            "GITHUB_SHA": "a" * 40,
            "ENVIRONMENT": json.dumps(environment),
            "CI_RUNS": json.dumps({"workflow_runs": runs}),
            "ENVIRONMENT_EXIT": str(environment_exit),
            "CI_EXIT": str(ci_exit),
        })

    def extension_inventory(self):
        return [
            {"id": name, "installedVersion": version, "version": "1.0.0-beta.999", "source": "azd"}
            for name, version in FOUNDRY_EXTENSIONS.items()
        ]

    def run_extension_setup(self, inventory, *, install_exit=0, list_exit=0):
        stub = """
azd() {
  printf 'AZD_CALL: %s\\n' "$*" >&2
  case "$1 $2" in
    "extension install") return "$INSTALL_EXIT" ;;
    "extension list") printf '%s\\n' "$EXTENSION_INVENTORY"; return "$LIST_EXIT" ;;
    "ai agent") return 0 ;;
    *) return 99 ;;
  esac
}
"""
        return self.run_workflow_step(self.extensions, stub, {
            "EXTENSION_INVENTORY": inventory,
            "INSTALL_EXIT": str(install_exit),
            "LIST_EXIT": str(list_exit),
        })

    def test_manual_default_branch_dispatch_requires_environment_approval(self):
        self.assertEqual(self.workflow["on"], {"workflow_dispatch": ""})
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["defaults"]["run"]["shell"], "bash")
        self.assertEqual(set(self.workflow["jobs"]), {"deploy-and-test"})
        self.assertNotIn("inputs.", self.source)
        self.assertNotIn("pull_request", self.source)
        self.assertEqual(self.deploy["environment"], "foundry-production")
        for condition in (
            "github.repository == 'microsoft/IssueLens'",
            "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
            "github.sha == github.workflow_sha",
        ):
            self.assertIn(condition, self.deploy["if"])

    def test_github_gates_run_before_oidc_login(self):
        gate = self.steps[1]
        self.assertEqual(gate["env"], {
            "GH_TOKEN": "${{ github.token }}",
            "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        })
        for condition in (
            '.can_admins_bypass == false', '.type == "required_reviewers"',
            "(.reviewers | length) > 0",
            "/actions/workflows/ci.yml/runs", "-f event=push",
            '-f branch="$DEFAULT_BRANCH"', '-f head_sha="$GITHUB_SHA"', "-F per_page=1",
            '.status == "completed" and .conclusion == "success"',
        ):
            self.assertIn(condition, gate["run"])
        self.assertEqual(gate["run"].count("grep -qx true"), 2)
        self.assertEqual(gate["run"].count("exit 1"), 2)
        login = next(index for index, step in enumerate(self.steps)
                     if step.get("uses", "").startswith("azure/login@"))
        self.assertGreater(login, 1)

    def test_github_gates_accept_both_environment_self_review_policies(self):
        for prevent_self_review in (True, False):
            with self.subTest(prevent_self_review=prevent_self_review):
                result = self.run_github_gates(
                    self.protected_environment(prevent_self_review),
                    [{"status": "completed", "conclusion": "success"}],
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr.count("GH_CALL:"), 2)
                self.assertIn(
                    "api --method GET repos/microsoft/IssueLens/actions/workflows/ci.yml/runs "
                    f"-f event=push -f branch=main -f head_sha={'a' * 40} -F per_page=1 --jq ",
                    result.stderr,
                )

    def test_github_gates_reject_missing_reviewers_and_admin_bypass(self):
        for prevent_self_review in (True, False):
            for change in ("missing rules", "empty rules", "wrong rule type",
                           "missing reviewers", "empty reviewers", "admin bypass"):
                with self.subTest(prevent_self_review=prevent_self_review, change=change):
                    environment = self.protected_environment(prevent_self_review)
                    rule = environment["protection_rules"][0]
                    if change == "missing rules":
                        del environment["protection_rules"]
                    elif change == "empty rules":
                        environment["protection_rules"] = []
                    elif change == "wrong rule type":
                        rule["type"] = "wait_timer"
                    elif change == "missing reviewers":
                        del rule["reviewers"]
                    elif change == "empty reviewers":
                        rule["reviewers"] = []
                    else:
                        environment["can_admins_bypass"] = True
                    result = self.run_github_gates(
                        environment, [{"status": "completed", "conclusion": "success"}],
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("::error::Configure required environment reviewers", result.stdout)
                    self.assertNotIn("/actions/workflows/ci.yml/runs", result.stderr)

    def test_github_gates_still_require_successful_ci_for_both_self_review_policies(self):
        for prevent_self_review in (True, False):
            for runs in (
                [], [{"status": "in_progress", "conclusion": "success"}],
                [{"status": "completed", "conclusion": conclusion}
                 for conclusion in ("failure", "success")],
                [{"status": "completed", "conclusion": "cancelled"}],
                [{"status": "completed", "conclusion": None}],
            ):
                with self.subTest(prevent_self_review=prevent_self_review, runs=runs):
                    result = self.run_github_gates(self.protected_environment(prevent_self_review), runs)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("::error::Wait for successful CI on this exact commit", result.stdout)

    def test_github_gates_stop_on_api_failure_even_with_successful_output(self):
        for environment_exit, ci_exit in ((9, 0), (0, 9)):
            with self.subTest(environment_exit=environment_exit, ci_exit=ci_exit):
                result = self.run_github_gates(
                    self.protected_environment(False),
                    [{"status": "completed", "conclusion": "success"}],
                    environment_exit=environment_exit, ci_exit=ci_exit,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr.count("GH_CALL:"), 1 if environment_exit else 2)
                self.assertIn("::error::", result.stdout)

    def test_official_oidc_and_azd_configuration_pattern(self):
        self.assertEqual(self.deploy["permissions"], {
            "contents": "read", "actions": "read", "id-token": "write",
        })
        login = next(step for step in self.steps if step.get("uses", "").startswith("azure/login@"))
        self.assertEqual(login["with"], {
            "client-id": "${{ secrets.AZURE_CLIENT_ID }}",
            "tenant-id": "${{ secrets.AZURE_TENANT_ID }}",
            "subscription-id": "${{ secrets.AZURE_SUBSCRIPTION_ID }}",
        })
        for filename in ("issue-triage.yml", "team-memory-post-merge.yml"):
            caller = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
            for reference in login["with"].values():
                self.assertIn(reference, caller)
        configure = next(step for step in self.steps if "azd config set" in step.get("run", ""))
        self.assertIn("azd config set auth.useAzCliAuth true", configure["run"])
        self.assertIn('azd env new "$AZD_ENV_NAME"', configure["run"])
        self.assertIn('azd env set --no-prompt -- "$name" "${!name}" || exit 1', configure["run"])
        self.assertIn('[[ -n "${!name}" ]]', configure["run"])
        for name in ("MAILING_URL", "PERSONAL_NOTIFICATION_URL"):
            self.assertEqual(configure["env"][name], "${{ secrets." + name + " }}")
        manifest = yaml.load((ROOT / "azure.yaml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for name in re.findall(r"\$\{([A-Z_]+)\}", str(manifest)):
            self.assertIn(name, configure["run"])
            self.assertIn(name, self.deploy["env"] | configure["env"])

    def test_azure_configuration_uses_step_scoped_secrets_and_existing_app_variable(self):
        self.assertEqual(self.deploy["env"], {"AZURE_CORE_OUTPUT": "none"})
        self.assertEqual(re.findall(r"vars\.([A-Z_]+)", self.source), ["ISSUELENS_APP_ID"])
        configure = next(step for step in self.steps if "azd config set" in step.get("run", ""))
        for name in (
            "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "AZURE_LOCATION", "FOUNDRY_PROJECT_ENDPOINT",
            "AZURE_AI_PROJECT_ID", "AZURE_AI_MODEL_DEPLOYMENT_NAME",
            "TOOLBOX_ENDPOINT", "MAILING_URL", "PERSONAL_NOTIFICATION_URL",
        ):
            self.assertEqual(configure["env"][name], "${{ secrets." + name + " }}")
        self.assertEqual(configure["env"]["GITHUB_APP_ID"], "${{ vars.ISSUELENS_APP_ID }}")
        self.assertEqual(configure["env"]["GITHUB_APP_PRIVATE_KEY_SECRET_URI"],
                         "${{ secrets.ISSUELENS_GITHUB_APP_PRIVATE_KEY_SECRET_URI }}")
        self.assertIn('>"$RUNNER_TEMP/issuelens-configure.log" 2>&1', configure["run"])
        self.assertIn("Details withheld.", configure["run"])

    def test_model_api_keys_are_not_used_by_deployment(self):
        self.assertNotIn("AZURE_AI_MODEL_API_KEY", self.source)

    def test_official_actions_and_bundle_are_pinned(self):
        for step in self.steps:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[A-Za-z0-9-]+/[A-Za-z0-9-]+@[0-9a-f]{40}$")
        self.assertEqual(self.steps[0]["with"], {"ref": "${{ github.sha }}", "persist-credentials": "false"})
        setup = next(step for step in self.steps if step.get("uses", "").startswith("Azure/setup-azd@"))
        self.assertEqual(setup["with"]["version"], "1.34.2")
        self.assertEqual(json.loads(self.extensions.get("env", {}).get("FOUNDRY_EXTENSION_VERSIONS", "{}")),
                         FOUNDRY_EXTENSIONS)
        self.assertIn('--version "$version" --source azd --no-dependencies --no-prompt', self.extensions["run"])
        self.assertIn("azd extension list --installed --output json", self.extensions["run"])
        login = next(index for index, step in enumerate(self.steps)
                     if step.get("uses", "").startswith("azure/login@"))
        self.assertLess(self.steps.index(self.extensions), login)
        self.assertIn("azd ai agent --help >/dev/null", self.commands)

    def test_extension_setup_installs_and_verifies_exact_versions_not_registry_latest(self):
        result = self.run_extension_setup(json.dumps(self.extension_inventory()[::-1]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [line for line in result.stderr.splitlines() if line.startswith("AZD_CALL:")]
        self.assertEqual(calls, [
            f"AZD_CALL: extension install {name} --version {version} --source azd --no-dependencies --no-prompt"
            for name, version in FOUNDRY_EXTENSIONS.items()
        ] + ["AZD_CALL: extension list --installed --output json", "AZD_CALL: ai agent --help"])

    def test_extension_setup_rejects_missing_or_changed_components(self):
        for index, name in enumerate(FOUNDRY_EXTENSIONS):
            for change in ("missing", "upgraded", "uninstalled"):
                with self.subTest(component=name, change=change):
                    inventory = self.extension_inventory()
                    if change == "missing":
                        del inventory[index]
                    else:
                        inventory[index]["installedVersion"] = "1.0.0-beta.999" if change == "upgraded" else ""
                    result = self.run_extension_setup(json.dumps(inventory))
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("::error::Foundry component versions do not match the reviewed pins.", result.stdout)
                    self.assertNotIn("AZD_CALL: ai agent", result.stderr)

    def test_extension_setup_rejects_extra_duplicate_and_invalid_inventories(self):
        inventory = self.extension_inventory()
        cases = {
            "extra": json.dumps(inventory + [{"id": "unexpected", "installedVersion": "1.0.0"}]),
            "duplicate": json.dumps(inventory + [inventory[0]]),
            "empty": "[]",
            "no output": "",
            "invalid JSON": "not JSON",
            "wrong shape": "{}",
            "null": "null",
            "multiple documents": json.dumps(inventory) + "\n" + json.dumps(inventory),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                result = self.run_extension_setup(payload)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("::error::Foundry component versions do not match the reviewed pins.", result.stdout)
                self.assertNotIn("AZD_CALL: ai agent", result.stderr)

    def test_extension_setup_stops_on_install_or_inventory_command_failure(self):
        for command in ("install", "list"):
            with self.subTest(command=command):
                result = self.run_extension_setup(
                    json.dumps(self.extension_inventory()),
                    install_exit=9 if command == "install" else 0,
                    list_exit=9 if command == "list" else 0,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("AZD_CALL: ai agent", result.stderr)
                if command == "install":
                    self.assertNotIn("AZD_CALL: extension list", result.stderr)

    def test_native_deployment_is_bounded_and_serialized(self):
        self.assertEqual(self.workflow["concurrency"], {
            "group": "issuelens-foundry-production", "cancel-in-progress": "false",
        })
        self.assertEqual(self.deploy["timeout-minutes"], "40")
        self.assertEqual(self.deploy["runs-on"], "ubuntu-24.04")
        deploy = next(step for step in self.steps if step.get("id") == "deploy")
        self.assertTrue(deploy["run"].startswith(
            'azd deploy IssueLens --environment "$AZD_ENV_NAME" --no-prompt --timeout 1200 >'))
        self.assertIn('>"$RUNNER_TEMP/issuelens-deploy.log" 2>&1', deploy["run"])
        self.assertIn("exit 1", deploy["run"])
        self.assertEqual(deploy["timeout-minutes"], "25")
        for step in self.steps:
            self.assertNotIn("continue-on-error", step)
        for forbidden in ("azd provision", "azd up", "azd init", "--from-package", "az role assignment"):
            self.assertNotIn(forbidden, self.commands)

    def test_both_protocols_check_the_new_version_and_expected_reply(self):
        status = next(step for step in self.steps if step.get("id") == "status")
        self.assertIn("AGENT_ISSUELENS_VERSION", status["run"])
        self.assertIn('.version == $version and .status == "active"', status["run"])
        for protocol in ("responses", "invocations"):
            step = next(step for step in self.steps if step.get("id") == protocol)
            self.assertEqual(step["timeout-minutes"], "3")
            self.assertEqual(step["env"], {"AGENT_VERSION": "${{ steps.status.outputs.version }}"})
            for text in (
                f"--protocol {protocol}", '--version "$AGENT_VERSION"', "--new-session",
                "--timeout 120", "--output raw", "ulimit -f 8192",
                'jq -e -s --arg expected "$EXPECTED_REPLY"', "exit 1",
            ):
                self.assertIn(text, step["run"])
        self.assertIn('--arg input "$AGENT_TEST_PROMPT" \'{input: $input}\'', self.commands)
        self.assertIn("--new-conversation", self.commands)
        self.assertIn('select(.type == "response.completed")', self.commands)
        self.assertIn('(.invocation_id | length) > 0', self.commands)
        self.assertIn(".data.content == $expected", self.commands)
        self.assertIn("Do not call tools or sub-agents", self.workflow["env"]["AGENT_TEST_PROMPT"])

    def test_summary_cleanup_and_secret_handling(self):
        summary, cleanup = self.steps[-2:]
        self.assertEqual(summary["if"], "always()")
        self.assertEqual(cleanup["if"], "always()")
        self.assertIn("GITHUB_STEP_SUMMARY", summary["run"])
        self.assertIn("No automatic retry or rollback", summary["run"])
        self.assertIn('"$AZD_ENV_NAME"', summary["run"])
        for name in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID"):
            self.assertNotIn(name, summary["run"])
        self.assertIn("rm -rf -- .azure", cleanup["run"])
        for name in ("issuelens-configure.log", "issuelens-deploy.log", "issuelens-status-errors.txt"):
            self.assertIn(name, cleanup["run"])
        self.assertNotIn("upload-artifact", self.source)
        self.assertNotIn("azd env get-values", self.commands)
        self.assertNotIn("cat ", self.commands)
        for step in self.steps:
            self.assertNotIn("${{", step.get("run", ""))

    def test_existing_manifest_needs_no_deployment_helper_or_hook(self):
        manifest = yaml.load((ROOT / "azure.yaml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        service = manifest["services"]["IssueLens"]
        self.assertEqual(service["codeConfiguration"], {
            "dependencyResolution": "remote_build", "entryPoint": "main.py", "runtime": "python_3_13",
        })
        self.assertNotIn("hooks", service)
        self.assertFalse((ROOT / ".github/scripts/foundry_deploy.py").exists())
        self.assertNotIn("python", self.commands)
        self.assertIn(".git", (ROOT / ".agentignore").read_text(encoding="utf-8").splitlines())

    def test_readme_documents_the_official_guide_and_all_configuration(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for reference in re.findall(r"(?:vars|secrets)\.([A-Z_]+)", self.source):
            self.assertIn(f"`{reference}`", readme)
        for text in (
            "https://learn.microsoft.com/en-us/azure/foundry/agents/quickstarts/set-up-cicd-hosted-agent",
            "foundry-production", "repo:microsoft/IssueLens:environment:foundry-production",
            "1.34.2", "1.0.0-beta.2",
            "No automatic retry or rollback", "No live deployment",
            "installedVersion", "--no-dependencies", "Bash and jq",
        ):
            self.assertIn(text, readme)
        for name, version in FOUNDRY_EXTENSIONS.items():
            self.assertIn(f"| `{name}` | `{version}` |", readme)


if __name__ == "__main__":
    unittest.main()
