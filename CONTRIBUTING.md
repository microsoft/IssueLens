# Contributing to IssueLens

Thanks for helping improve IssueLens. Contributions to code, agent behavior,
tests, documentation, and examples are welcome.

## Before you start

- Search existing issues and pull requests before starting work. For a larger
  change, open an issue to discuss the problem and proposed approach.
- For bug reports, include reproduction steps, expected and actual behavior,
  and relevant versions. Remove credentials and private repository content
  from examples and logs.
- Report security vulnerabilities through [SECURITY.md](SECURITY.md), not a
  public issue.

## Development setup

Fork and clone the repository, then create a branch for your change. Use
**Python 3.13** to match the hosted runtime. The standalone MCP package also
supports Python 3.12.

Run commands from the repository root. In Bash:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-ci.txt
```

Or in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-ci.txt
```

**You do not need a `.env` file, Azure credentials, a GitHub App, or a live model
to run the tests.** Dependencies require network access to install; tests use
local fixtures and mocked services.

To run the agent itself, follow the separate [local setup guide](docs/guide.md#running-locally).
Live requests may perform authorized writes, so use a test repository. Never
commit tokens, private keys, `.env` files, or secret-bearing notification URLs.
Editing or testing a change does not authorize deployment; deployment requires
separate explicit approval.

## Find your way around

| Path | Purpose |
| --- | --- |
| `main.py` | Hosted server, protocol handling, and explicit agent/skill registration |
| `agents/` | Runtime prompts for the orchestrator and four specialist agents |
| `skills/` | Capability instructions for triage, planning, change analysis, and memory |
| `github_app_mcp/` | GitHub App MCP server, packaging, and isolated tests |
| `tests/` | Application, prompt-contract, workflow, and documentation tests |
| `.github/actions/issuelens/` | Reusable GitHub Actions integration and request helpers |
| `.github/workflows/` | CI, deployment, and operational workflows |
| `examples/` and `schemas/` | Target-repository configuration examples and schema |
| `docs/` and `observability/` | Setup, operations, and reporting resources |

For protocol and operational details, see the [setup and usage guide](docs/guide.md).
For tool contracts and standalone package development, see the
[MCP reference](github_app_mcp/README.md).

## Changing agents and skills

The `issuelens` orchestrator routes work to `triage`, `find-criticals`, `plan`,
and `team-memory`. Prompts live under `agents/` and are loaded explicitly in
`main.py` for both protocols. Prefer changing the owning skill or agent prompt
before adding host logic, and update the corresponding tests.

Runtime prompts are application assets, not contributor instructions. Their
restrictions on implementation, pull requests, and deployment describe the
deployed product, not contributors maintaining it. Keep runtime prompts out of
`AGENTS.md` (including lowercase `agents.md`); the orchestrator prompt stays in
`agents/issuelens.md`. Repository maintenance guidance is in
[`.github/copilot-instructions.md`](.github/copilot-instructions.md).

To add a skill, create `skills/<name>/SKILL.md` with frontmatter and instructions:

```markdown
---
name: my-skill
description: What this skill does.
---

# My skill

Instructions for the agent when this skill is active.
```

Register new runtime components explicitly in `main.py` where needed. Preserve
role ownership: triage owns issue follow-up, planning owns planning artifacts,
and only the team-memory agent owns separately authorized wiki maintenance.
All agents can retrieve memory through the shared read-only skill.

Keep behavior consistent across chat and automation. Preserve scoped GitHub
App access, explicit write authorization, validated repository policy, and
wiki destination/base preconditions. Tools perform one logical operation;
recovery belongs to the owning agent, and uncertain writes must not be reported
as successful. The [MCP execution contract](github_app_mcp/README.md#execution-contract)
documents these boundaries.

## Run checks

Start with the tests covering your change, then run the relevant suite before
submitting. For example, run one application test module with:

```bash
python -m unittest discover -s tests -p 'test_issue_triage_workflow.py' -v
```

The complete application checks are:

```bash
python -m compileall -q *.py .github/actions/issuelens github_app_mcp/src github_app_mcp/scripts tests github_app_mcp/tests
python -m unittest discover -s tests -p 'test_*.py' -v
```

The syntax command above uses Bash glob expansion. In PowerShell, pass root
Python files explicitly:

```powershell
$rootModules = Get-ChildItem -File -Filter *.py | Select-Object -ExpandProperty Name
python -m compileall -q $rootModules .github\actions\issuelens github_app_mcp\src github_app_mcp\scripts tests github_app_mcp\tests
```

### Standalone MCP tests and package checks

Use isolated environments so application dependencies do not mask package
issues. Run these Bash commands with Python 3.12, then repeat with `python3.13`
in place of `python3.12`:

```bash
MCP_CHECK_DIR="$(mktemp -d)"
python3.12 -m venv "$MCP_CHECK_DIR/tests"
(
  source "$MCP_CHECK_DIR/tests/bin/activate"
  python -m pip install ./github_app_mcp -r requirements-ci.txt
  python -m unittest discover -s github_app_mcp/tests -p 'test_*.py' -v
)
python3.12 -m venv "$MCP_CHECK_DIR/build"
(
  source "$MCP_CHECK_DIR/build/bin/activate"
  python -m pip install -r requirements-ci.txt
  python -m build --outdir "$MCP_CHECK_DIR/dist" github_app_mcp
  python -m venv "$MCP_CHECK_DIR/wheel"
  "$MCP_CHECK_DIR/wheel/bin/python" -m pip install -c constraints-ci.txt "$MCP_CHECK_DIR"/dist/*.whl
  "$MCP_CHECK_DIR/wheel/bin/python" -m pip check
  "$MCP_CHECK_DIR/wheel/bin/python" -I - <<'PY'
from importlib.metadata import distribution

dist = distribution("issuelens-github-mcp")
entry, = (ep for ep in dist.entry_points if ep.group == "console_scripts" and ep.name == "issuelens-github-mcp")
assert entry.value == "issuelens_github_mcp.server:main"
assert callable(entry.load())
print(f"Installed {dist.metadata['Name']} {dist.version}; entry point imports successfully")
PY
)
rm -rf "$MCP_CHECK_DIR"
```

### Workflow validation

[actionlint](https://github.com/rhysd/actionlint) v1.7.12 checks workflow syntax,
expressions, job dependencies, and action inputs. With Go 1.25+ installed, run
these Bash commands:

```bash
go install github.com/rhysd/actionlint/cmd/actionlint@914e7df21a07ef503a81201c76d2b11c789d3fca
"$(go env GOPATH)/bin/actionlint" -shellcheck= -pyflakes= .github/workflows/*.yml
```

Optional ShellCheck and Pyflakes integrations are disabled to keep local and CI
scope the same. [`.github/actionlint.yaml`](.github/actionlint.yaml) suppresses
only the unknown `copilot-requests` permission diagnostic in the manual billing
probe and the unrecognized `queue` concurrency key in the team-memory
coordinator. GitHub supports `queue: max`, but actionlint v1.7.12 does not;
`test_team_memory_workflow.py` separately checks the exact fixed queue group,
`queue: max`, and `cancel-in-progress: false`. All other workflow diagnostics
remain enabled. Remove these file-specific exceptions when actionlint supports
the features. Deployment
workflow regression tests also require Bash and jq, available on CI's Ubuntu
runner; run those checks in a matching environment.

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull request
and push to `main`, separately from operational workflows.

| Check | Scope |
| --- | --- |
| `Application tests (Python 3.13)` | Python syntax validation and all `tests/`; matches the hosted runtime. |
| `MCP tests (Python 3.12)` | All `github_app_mcp/tests/` at the standalone package's minimum Python version. |
| `MCP tests (Python 3.13)` | The same MCP suite on the hosted Python version. |
| `MCP package build/install (Python 3.12)` | Build an sdist, build its wheel, install into a clean environment, check dependencies and import the console entry point. |
| `MCP package build/install (Python 3.13)` | The same distributable validation on Python 3.13. |
| `Workflow validation` | actionlint validation of all `.github/workflows/*.yml`. |

CI uses ordinary `pull_request`, not `pull_request_target`, with only
`contents: read`, no persisted checkout credentials, and no secrets or
environment approvals. Fork PRs are supported, subject to GitHub's maintainer
approval for first-time contributors. CI does not invoke IssueLens, deploy
agents, publish packages, or write issues or wikis. The wheel smoke test imports
but does not start the MCP server.

Jobs have 10-15 minute timeouts; newer runs cancel superseded runs for the same
PR/ref. Python jobs cache pip downloads by runtime and dependency manifests.
Matrix failures remain failing checks without cancelling the other Python
version. Maintainers can make the check names above required after observing
successful Actions runs. Local checks do not establish live Actions execution,
OIDC/RBAC access, hosted readiness, or wiki publication.

### Dependency and tool versions

[`requirements-ci.txt`](requirements-ci.txt) pins PyYAML, `packaging`, and PyPA
`build`. [`constraints-ci.txt`](constraints-ci.txt), loaded by the CI
requirements and clean wheel installation, fixes the tested MCP baseline at
`mcp==2.0.1`, within both runtime manifests' supported range. This is not a full
dependency lock or coverage of every newer MCP release. MCP 2.2.0 masks
tool-error details asserted by existing wiki protocol tests; review the
constraint when updating SDK support rather than weakening those assertions.
`compileall` checks syntax, not formatting, types, or style.

Actions and actionlint source are pinned to immutable commits. Dependabot
proposes action updates; review upgrades, update version comments, pins, tests,
and documentation together, then rerun validation. Go is used only to install
actionlint; Go caching is disabled because this repository has no Go module.

## Submit a pull request

Keep changes focused and include a clear explanation of the problem, approach,
and any behavior changes. Link the relevant issue, add or update tests, and
describe the checks you ran and any limitations. Update documentation and
examples when configuration, commands, tool contracts, or behavior change.

Review your diff for secrets, unrelated edits, and generated files before
opening the pull request. Keep the [README](README.md) a short project overview;
put contributor instructions here and setup or operational details in the
[guide](docs/guide.md).
