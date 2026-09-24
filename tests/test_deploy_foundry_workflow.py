import pathlib
import re
import unittest

import yaml


ROOT = pathlib.Path(__file__).parents[1]


class FoundryDeploymentWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / ".github/workflows/deploy-foundry.yml").read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.source, Loader=yaml.BaseLoader)
        cls.deploy = cls.workflow["jobs"]["deploy-and-test"]
        cls.steps = cls.deploy["steps"]
        cls.commands = "\n".join(step.get("run", "") for step in cls.steps)

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
            ".prevent_self_review == true", "(.reviewers | length) > 0",
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
        self.assertIn("azd extension install microsoft.foundry --version 1.0.0-beta.2 --no-prompt", self.commands)
        self.assertIn("azd ai agent --help >/dev/null", self.commands)

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
        ):
            self.assertIn(text, readme)


if __name__ == "__main__":
    unittest.main()
