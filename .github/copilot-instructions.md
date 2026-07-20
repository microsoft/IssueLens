# IssueLens — Copilot instructions

IssueLens is a **GitHub issue-triage agent** that runs as a **Microsoft Foundry
hosted agent**, built on the **GitHub Copilot SDK**. It analyzes issues across
repositories, identifies critical (hot / blocking / regression) issues, applies
labels, and sends notifications — acting on GitHub as a **GitHub App**, so all
writes are attributed to the App bot with the App's scoped permissions.

## Main features

- **Critical-issue triage** — a dedicated *Critical Issue Analyst* sub-agent
  scans issues updated within the last 24 hours (configurable via
  `TRIAGE_WINDOW_HOURS`, default: `24`) and returns a structured JSON report
  identifying **hot**, **blocking**, and **regression** issues.
- **Duplicate detection** — compares a target issue with open and closed issues
  using strict technical evidence and reports high-confidence duplicates.
- **Auto-labeling** — classifies and applies labels using the repository's
  existing labels (and optional `.github/label-instructions.md`).
- **Auto-assignment** — routes issues to individual owners using repository area
  mappings and historical assignment patterns.
- **Notifications** — sends triage reports via **WorkIQ** (Microsoft 365) as
  email or Teams messages.
- **App-scoped GitHub access** — every GitHub action flows through the GitHub MCP
  server using a per-request GitHub App installation token (attributed to the
  App bot, never an ambient user identity).

## Architecture

- **`main.py`** — the Foundry hosted-agent server (`InvocationAgentServerHost`,
  Starlette + SSE). Exposes `POST /invocations`; each request opens a fresh
  Copilot session and streams session events back as SSE.
  - **Invocation payload:** exactly two fields —
    `{ "input": "<free-form task>", "github_token": "<token>" }`. `input` is the
    task; `github_token` authenticates the GitHub MCP server.
    If the GitHub MCP server returns an authentication or permission error,
    respond immediately with HTTP 400 and body
    `{ "error": "github_token invalid or insufficient scopes" }`; do not proceed
    with any GitHub operations.
  - **Model (inference) auth (auto-selected):** BYOK Foundry model
    (`FOUNDRY_PROJECT_ENDPOINT` + `AZURE_AI_MODEL_DEPLOYMENT_NAME`, using
    `AZURE_AI_MODEL_API_KEY` or a Managed Identity token), or the GitHub Copilot
    model (`GITHUB_TOKEN`). If `FOUNDRY_PROJECT_ENDPOINT` is set, always use the
    BYOK Foundry model; fall back to the GitHub Copilot model (`GITHUB_TOKEN`)
    only when `FOUNDRY_PROJECT_ENDPOINT` is absent.
- **Custom agents** (registered in `main.py`):
  - **`issuelens`** — the orchestrator. Delegates analysis to the analyst, then
    performs duplicate detection, labeling, assignment, and notifications via
    its preloaded skills. If the
    analyst's response is not valid JSON or is an empty object, stop, skip
    labeling and notifications, and surface this error message:
    `Triage report could not be parsed; skipping downstream actions.`
  - **`critical-issue-analyst`** — the triage sub-agent; its prompt lives in
    `agents/critical-issue-analyst.md` and it returns **only** the JSON report.
- **Skills** (`skills/`): `find-duplicates` (identify duplicate and related
  issues via GitHub MCP tools), `label-issue` (apply labels via GitHub MCP
  tools), `assign-issue` (route and assign issues via GitHub MCP tools), and
  `notify` (send the report via the `workiq-*` MCP tools).
- **GitHub access** — the remote GitHub MCP server
  (`https://api.githubcopilot.com/mcp/`), authenticated with the payload's
  `github_token`.
- **Config auto-discovery** — the Copilot harness discovers supporting config
  from the project root, so capabilities can be added **without editing
  `main.py`**:
  - Instructions: `AGENTS.md`, `.github/copilot-instructions.md`, `*.instructions.md`
  - MCP servers: `.mcp.json`, `.vscode/mcp.json` (WorkIQ tools come from the
    Foundry toolbox)
  - Skills: `skills/` · Hooks: `.github/hooks/` · Plugins: plugin directories

## Conventions

- **All GitHub access goes through the GitHub MCP server** — never shell out to
  `gh` / `bash` / `powershell` for GitHub operations. This keeps writes
  attributed to the App bot. See `AGENTS.md`.
- The analyst returns **only** the JSON object (no prose); the orchestrator
  preserves that report at the very end of its response.
- Prefer adding a **skill, agent, or instruction file** (picked up by config
  discovery) over changing `main.py`.

## Triggering (GitHub Actions)

The agent is driven by a workflow in the target repo
(`.github/workflows/issuelens.yml`):

1. Mint a GitHub App installation token with `actions/create-github-app-token`.
2. Authenticate to the Foundry agent endpoint via **Azure OIDC** (`azure/login`).
3. POST `{ input, github_token }` to the agent's invocations endpoint.

Triggers: `issues` opened/reopened (label the issue), `schedule` (batch triage),
and `workflow_dispatch`.

## Run & deploy

- **Local:** `pip install -r requirements.txt`, copy `.env.example` → `.env` and
  fill it in, then `python main.py` (serves the invocations endpoint on `:8088`).
- **Deploy to Foundry:** `azd deploy` — see `azure.yaml` (Python hosted agent,
  `codeConfiguration` remote build) and `agent.yaml` (hosted-agent manifest). A
  `Dockerfile` is also provided for a container build.

## Layout

- `main.py` — agent server, session wiring, custom-agent registration
- `agents/` — sub-agent prompts (`critical-issue-analyst.md`)
- `skills/` — modular skills (`find-duplicates`, `label-issue`, `assign-issue`,
  `notify`)
- `AGENTS.md` — agent behavior rules
- `azure.yaml` / `agent.yaml` / `Dockerfile` — deployment config
- `.github/workflows/issuelens.yml` — the triggering workflow
