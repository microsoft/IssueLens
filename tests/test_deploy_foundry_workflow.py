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
        cls.preflight = cls.workflow["jobs"]["preflight"]
        cls.deploy = cls.workflow["jobs"]["deploy"]

    def test_dispatch_has_no_untrusted_target_or_revision_inputs(self):
        self.assertEqual(self.workflow["on"], {"workflow_dispatch": ""})
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["defaults"]["run"]["shell"], "bash")
        self.assertNotIn("inputs.", self.source)
        self.assertNotIn("pull_request", self.source)
        self.assertEqual(self.deploy["needs"], "preflight")
        self.assertEqual(self.deploy["environment"], "foundry-production")

    def test_branch_guard_runs_before_checkout(self):
        guard, checkout, check = self.preflight["steps"]
        for condition in (
            '"$GITHUB_REPOSITORY" != "microsoft/IssueLens"',
            '"$GITHUB_EVENT_NAME" != "workflow_dispatch"',
            '"$GITHUB_REF" != "refs/heads/$DEFAULT_BRANCH"',
            '"$GITHUB_SHA" != "$WORKFLOW_SHA"',
        ):
            self.assertIn(condition, guard["run"])
        self.assertIn("exit 1", guard["run"])
        self.assertEqual(guard["env"]["WORKFLOW_SHA"], "${{ github.workflow_sha }}")
        self.assertEqual(guard["env"]["DEFAULT_BRANCH"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertTrue(check["run"].endswith("foundry_deploy.py preflight"))
        self.assertEqual(check["env"], {"GH_TOKEN": "${{ github.token }}"})

    def test_preflight_is_unprivileged_and_deploy_rechecks_before_login(self):
        read_permissions = {"contents": "read", "actions": "read", "deployments": "read"}
        self.assertEqual(self.preflight["permissions"], read_permissions)
        self.assertEqual(self.deploy["permissions"], {**read_permissions, "id-token": "write"})
        self.assertNotIn("secrets.", str(self.preflight))
        steps = self.deploy["steps"]
        self.assertTrue(steps[1]["run"].endswith("foundry_deploy.py preflight"))
        prepare = next(index for index, step in enumerate(steps) if step.get("run", "").endswith(" prepare"))
        login = next(index for index, step in enumerate(steps) if step.get("uses", "").startswith("azure/login@"))
        publish = next(index for index, step in enumerate(steps) if step.get("id") == "publish")
        self.assertLess(prepare, login)
        self.assertLess(login, publish)
        identity = steps[login]["with"]
        self.assertEqual(identity, {
            "client-id": "${{ secrets.ISSUELENS_DEPLOY_AZURE_CLIENT_ID }}",
            "tenant-id": "${{ secrets.ISSUELENS_DEPLOY_AZURE_TENANT_ID }}",
            "subscription-id": "${{ secrets.ISSUELENS_DEPLOY_AZURE_SUBSCRIPTION_ID }}",
        })
        for step in steps:
            if "env" in step and step.get("run", "").endswith(" prepare"):
                for name in ("AZURE_AI_MODEL_API_KEY", "MAILING_URL", "PERSONAL_NOTIFICATION_URL"):
                    self.assertEqual(step["env"][name], "${{ secrets." + name + " }}")
            self.assertNotIn("continue-on-error", step)

    def test_actions_and_deployment_provider_are_pinned(self):
        for job in (self.preflight, self.deploy):
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"^[A-Za-z0-9-]+/[A-Za-z0-9-]+@[0-9a-f]{40}$")
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertEqual(step["with"], {"ref": "${{ github.sha }}", "persist-credentials": "false"})
        setup = next(step for step in self.deploy["steps"] if step.get("uses", "").startswith("Azure/setup-azd@"))
        self.assertEqual(setup["with"]["version"], "1.34.2")
        install = next(step["run"] for step in self.deploy["steps"]
                       if step.get("run", "").startswith("azd extension"))
        self.assertEqual(install,
                         "azd extension install azure.ai.agents --version 1.0.0-beta.16 --no-dependencies --no-prompt")

    def test_one_target_is_serialized_without_cancelling_publication(self):
        self.assertEqual(self.workflow["concurrency"], {
            "group": "issuelens-foundry-production", "cancel-in-progress": "false",
        })
        self.assertEqual(self.preflight["timeout-minutes"], "5")
        self.assertEqual(self.deploy["timeout-minutes"], "40")
        for job in self.workflow["jobs"].values():
            self.assertEqual(job["runs-on"], "ubuntu-24.04")
            self.assertNotIn("strategy", job)

    def test_summary_and_cleanup_run_after_failures(self):
        summary, cleanup = self.deploy["steps"][-2:]
        self.assertEqual(summary["if"], "always()")
        self.assertEqual(cleanup["if"], "always()")
        self.assertTrue(summary["run"].endswith("foundry_deploy.py summary"))
        self.assertTrue(cleanup["run"].endswith("foundry_deploy.py cleanup"))
        self.assertEqual(summary["env"], {
            "PUBLISH_OUTCOME": "${{ steps.publish.outcome }}",
            "VERIFY_OUTCOME": "${{ steps.verify.outcome }}",
        })
        self.assertNotIn("upload-artifact", self.source)
        for step in self.deploy["steps"]:
            self.assertNotIn("${{", step.get("run", ""))

    def test_ci_only_postpackage_hook_preserves_the_native_deploy_path(self):
        manifest = yaml.load((ROOT / "azure.yaml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        service = manifest["services"]["IssueLens"]
        self.assertEqual(service["codeConfiguration"], {
            "dependencyResolution": "remote_build", "entryPoint": "main.py", "runtime": "python_3_13",
        })
        self.assertEqual(set(service["hooks"]), {"postpackage"})
        hook = service["hooks"]["postpackage"]
        self.assertEqual(hook["posix"]["shell"], "sh")
        self.assertEqual(hook["windows"]["shell"], "pwsh")
        for platform in ("posix", "windows"):
            self.assertIn("ISSUELENS_PACKAGE_CHECK_DIR", hook[platform]["run"])
            self.assertIn("foundry_deploy.py package-check", hook[platform]["run"])
            self.assertNotIn("continueOnError", hook[platform])
        helper = (ROOT / ".github/scripts/foundry_deploy.py").read_text(encoding="utf-8")
        for forbidden in ('"provision"', '"--from-package"', '"up"', '"role", "assignment"', '"keyvault"'):
            self.assertNotIn(forbidden, helper)

    def test_readme_documents_configuration_and_manual_activation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        prepare = next(step for step in self.deploy["steps"] if step.get("run", "").endswith(" prepare"))
        for reference in re.findall(r"(?:vars|secrets)\.([A-Z_]+)", str(prepare["env"])):
            self.assertIn(f"`{reference}`", readme)
        for text in (
            "foundry-production", "repo:microsoft/IssueLens:environment:foundry-production",
            "1.34.2", "1.0.0-beta.16", "azd-code-deploy-", "postpackage",
            "No automatic retry or rollback", "No live deployment",
        ):
            self.assertIn(text, readme)


if __name__ == "__main__":
    unittest.main()
