import pathlib
import re
import unittest

import yaml


ROOT = pathlib.Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.source, Loader=yaml.BaseLoader)
        cls.jobs = cls.workflow["jobs"]

    def test_runs_on_all_pull_requests_and_main_pushes(self):
        self.assertEqual(self.workflow["name"], "CI")
        self.assertEqual(
            self.workflow["on"],
            {"pull_request": "", "push": {"branches": ["main"]}},
        )
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(self.workflow["defaults"]["run"]["shell"], "bash")

    def test_check_names_and_runtime_coverage_are_stable(self):
        self.assertEqual(
            {key: job["name"] for key, job in self.jobs.items()},
            {
                "application-tests": "Application tests (Python 3.13)",
                "mcp-tests": "MCP tests (Python ${{ matrix.python-version }})",
                "mcp-package": "MCP package build/install (Python ${{ matrix.python-version }})",
                "workflow-validation": "Workflow validation",
            },
        )
        for key in ("mcp-tests", "mcp-package"):
            self.assertEqual(self.jobs[key]["strategy"]["matrix"]["python-version"], ["3.12", "3.13"])
            self.assertEqual(self.jobs[key]["strategy"]["fail-fast"], "false")
        setup = self.python_setup("application-tests")
        self.assertEqual(setup["with"]["python-version"], "3.13")

    def test_runs_are_bounded_and_superseded_runs_are_cancelled(self):
        self.assertEqual(
            self.workflow["concurrency"],
            {
                "group": "ci-${{ github.repository }}-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
                "cancel-in-progress": "true",
            },
        )
        for job in self.jobs.values():
            self.assertEqual(job["runs-on"], "ubuntu-24.04")
            self.assertGreater(int(job["timeout-minutes"]), 0)
            self.assertLessEqual(int(job["timeout-minutes"]), 15)
            for key in ("if", "continue-on-error", "needs", "environment", "permissions"):
                self.assertNotIn(key, job)
            for step in job["steps"]:
                self.assertNotIn("continue-on-error", step)
                self.assertNotIn("if", step)

    def test_actions_are_pinned_and_checkout_drops_credentials(self):
        for job in self.jobs.values():
            checkouts = []
            for step in job["steps"]:
                if "uses" not in step:
                    continue
                self.assertRegex(step["uses"], r"^actions/(checkout|setup-python|setup-go)@[0-9a-f]{40}$")
                if step["uses"].startswith("actions/checkout@"):
                    checkouts.append(step)
                    self.assertEqual(step["with"], {"persist-credentials": "false"})
            self.assertEqual(len(checkouts), 1)

    def test_ci_has_no_operational_or_credentialed_paths(self):
        for forbidden in (
            "pull_request_target", "secrets.", "github.token", "github-script",
            "id-token", "azure/login", "AZURE_", "FOUNDRY_", "GITHUB_APP_",
            "ISSUELENS_AGENT_", "GITHUB_TOKEN", "./.github/actions/issuelens",
            "azd ", "python main.py", "write_wiki", "gh api",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def python_setup(self, job_id):
        return next(step for step in self.jobs[job_id]["steps"]
                    if step.get("uses", "").startswith("actions/setup-python@"))

    def commands(self, job_id):
        return "\n".join(step["run"] for step in self.jobs[job_id]["steps"] if "run" in step)

    def test_pip_caches_include_all_installed_manifests(self):
        for job_id, manifest in (
            ("application-tests", "requirements.txt"),
            ("mcp-tests", "github_app_mcp/pyproject.toml"),
            ("mcp-package", "github_app_mcp/pyproject.toml"),
        ):
            setup = self.python_setup(job_id)["with"]
            self.assertEqual(setup["cache"], "pip")
            self.assertEqual(set(setup["cache-dependency-path"].split()), {manifest, "requirements-ci.txt"})
            if job_id != "application-tests":
                self.assertEqual(setup["python-version"], "${{ matrix.python-version }}")

    def test_both_suites_run_directly_so_failures_fail_the_job(self):
        for job_id, command in (
            ("application-tests", "python -m unittest discover -s tests -p 'test_*.py' -v"),
            ("mcp-tests", "python -m unittest discover -s github_app_mcp/tests -p 'test_*.py' -v"),
        ):
            self.assertIn(command, [step.get("run") for step in self.jobs[job_id]["steps"]])
        self.assertIn(
            "python -m pip install -r requirements.txt -r requirements-ci.txt",
            self.commands("application-tests"),
        )
        self.assertIn(
            "python -m pip install ./github_app_mcp -r requirements-ci.txt",
            self.commands("mcp-tests"),
        )

    def test_package_build_and_install_are_isolated_from_source_imports(self):
        commands = self.commands("mcp-package")
        for command in (
            'python -m build --outdir "$RUNNER_TEMP/dist" github_app_mcp',
            'python -m venv "$RUNNER_TEMP/wheel-venv"',
            '"$RUNNER_TEMP/wheel-venv/bin/python" -m pip install "$RUNNER_TEMP"/dist/*.whl',
            '"$RUNNER_TEMP/wheel-venv/bin/python" -m pip check',
            '"$RUNNER_TEMP/wheel-venv/bin/python" -I -',
            'distribution("issuelens-github-mcp")',
            'assert entry.value == "issuelens_github_mcp.server:main"',
            "assert callable(entry.load())",
        ):
            self.assertIn(command, commands)
        self.assertNotIn("pip install ./github_app_mcp", commands)
        self.assertNotIn("--system-site-packages", commands)

    def test_validation_tools_and_commands_are_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        syntax = "python -m compileall -q *.py .github/actions/issuelens github_app_mcp/src github_app_mcp/scripts tests github_app_mcp/tests"
        self.assertIn(syntax, self.commands("application-tests"))
        self.assertIn(syntax, readme)
        lint = self.commands("workflow-validation")
        install = re.search(r"go install github.com/rhysd/actionlint/cmd/actionlint@[0-9a-f]{40}", lint)
        self.assertIsNotNone(install)
        self.assertIn(install.group(), readme)
        command = '"$(go env GOPATH)/bin/actionlint" -shellcheck= -pyflakes= .github/workflows/*.yml'
        self.assertIn(command, lint)
        self.assertIn(command, readme)
        for job in self.jobs.values():
            for version in ("3.12", "3.13") if "strategy" in job else ("3.13",):
                self.assertIn(job["name"].replace("${{ matrix.python-version }}", version), readme)
        for job_id in ("application-tests", "mcp-tests"):
            for step in self.jobs[job_id]["steps"]:
                if "run" in step:
                    self.assertIn(step["run"], readme)

    def test_actionlint_exception_is_limited_to_existing_billing_permission(self):
        config = yaml.load(
            (ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(config, {
            "paths": {
                ".github/workflows/copilot-cli-org-billing-test.yml": {
                    "ignore": [r'^unknown permission scope "copilot-requests"\.'],
                },
            },
        })


if __name__ == "__main__":
    unittest.main()
