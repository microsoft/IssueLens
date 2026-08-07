# IssueLens — Copilot instructions

IssueLens is a **GitHub issue-triage agent** that runs as a **Microsoft Foundry
hosted agent**, built on the **GitHub Copilot SDK**. It analyzes issues across
repositories, identifies critical (hot / blocking / regression) issues, applies
labels, and sends notifications — acting on GitHub as a **GitHub App**, so all
writes are attributed to the App bot with the App's scoped permissions. It is
reachable two ways: the **invocations** protocol for automation and the
**responses** protocol for chat.

## Main features

- **Critical-issue triage** — the `find-criticals` sub-agent
  scans issues updated within the requested time scope (default: the last 24
  hours) and returns a structured JSON report
  identifying **hot**, **blocking**, and **regression** issues.
- **Duplicate detection** — compares a target issue with open and closed issues
  using strict technical evidence and reports high-confidence duplicates.
- **Auto-labeling** — classifies and applies labels using the repository's
  existing labels (and optional `.github/label-instructions.md`).
- **Auto-assignment** — routes issues to individual owners using repository area
  mappings and historical assignment patterns.
- **Notifications** — sends triage reports via **WorkIQ** (Microsoft 365) as
  email or Teams messages.
- **App-scoped GitHub access** — invocations use GitHub MCP with the request
  token; chat uses one skill-backed in-process tool. Both act as the App bot,
  never an ambient user identity.

## Architecture

- **`main.py`** — the Foundry hosted-agent server. A single host class
  (`IssueLensHost(InvocationAgentServerHost, ResponsesAgentServerHost)`) serves
  two protocols in one process:
  - **`POST /invocations`** — automation (GitHub Actions). Each request opens a
    fresh Copilot session and streams session events back as SSE.
    **Invocation payload:** two required fields and one optional field —
    `{ "input": "<free-form task>", "github_token": "<token>", "attachments": [] }`.
    `input` is the task; `github_token` authenticates the GitHub MCP server;
    `attachments` contains validated inline Copilot `blob` attachments.
    If the GitHub MCP server returns an authentication or permission error,
    respond immediately with HTTP 400 and body
    `{ "error": "github_token invalid or insufficient scopes" }`; do not proceed
    with any GitHub operations.
  - **`POST /responses`** — chat (playground, Teams, any Responses client).
    There is no payload token, so the `github-access` tool loads the App private key
    from Azure Key Vault, resolves the installation for each target repository,
    and caches its short-lived token. The token never enters model context. The
    conversation's Copilot session is resumed each turn.
  - **Issue-body images** — before the model turn, the trusted host loader
    resolves explicit issue URLs or `owner/repository#number` references using
    the protocol's GitHub identity, accepts only allowlisted GitHub-hosted image
    URLs, validates redirects, type signatures, count, and size, and supplies
    Copilot blob attachments. It never exposes tokens or lets the model choose
    arbitrary download URLs.
  - **Model (inference) auth (auto-selected):** BYOK Foundry model
    (`FOUNDRY_PROJECT_ENDPOINT` + `AZURE_AI_MODEL_DEPLOYMENT_NAME`, using
    `AZURE_AI_MODEL_API_KEY` or a Managed Identity token), or the GitHub Copilot
    model (`GITHUB_TOKEN`). If `FOUNDRY_PROJECT_ENDPOINT` is set, always use the
    BYOK Foundry model; fall back to the GitHub Copilot model (`GITHUB_TOKEN`)
    only when `FOUNDRY_PROJECT_ENDPOINT` is absent.
- **Custom agents** (registered in `main.py`):
  - **`issuelens`** — the global agent identity. Its system prompt lives in
    `agents.md`. It routes issue-level analysis to `triage` and critical-issue
    scans to `find-criticals`. If the
    sub-agent's response is not valid JSON or is an empty object, stop, skip
    labeling and notifications, and surface this error message:
    `Triage report could not be parsed; skipping downstream actions.`
  - **`triage`** — analyzes target issues and performs requested duplicate,
    label, assignment, and notification work through its preloaded skills. Its
    prompt lives in `agents/triage.md`.
  - **`find-criticals`** — scans a repository and time scope for hot, blocking,
    and regression issues. Its prompt lives in `agents/find-criticals.md` and it
    returns **only** the critical-issue JSON report.
- **Skills** (`skills/`): `github-access` (chat GitHub App operations),
  `find-duplicates`, `label-issue`, `assign-issue`, and `notify`.
- **Media inputs** — `media_inputs.py` normalizes Responses `input_image` and
  `input_file` content and invocation `blob` attachments into Copilot session
  attachments. Only inline base64 content is accepted; remote URLs, file IDs,
  and request-supplied server paths are rejected.
- **GitHub access** — invocations use the remote GitHub MCP server
  (`https://api.githubcopilot.com/mcp/`) authenticated with the payload's
  `github_token`; chat uses only the skill-owned `github-access` tool. The host
  image loader uses the same protocol-specific identity. Follow the
  `github-access` skill before every GitHub read or write.
- **Runtime configuration** — `main.py` explicitly loads `agents.md`,
  both sub-agent prompts under `agents/`, and the skill directories. Explicit
  loading keeps local and hosted behavior identical without enabling config
  discovery in the read-only hosted code directory.

## Conventions

- **GitHub access follows the protocol boundary** — first follow the
  `github-access` skill; invocations then use GitHub MCP and chat uses only the
  `github-access` tool. Never shell out to `gh` / `bash` / `powershell`, call
  GitHub over direct HTTP, or expose App credentials. See `agents.md`.
- Only `find-criticals` is required to return JSON, which IssueLens preserves at
  the end of its response. `triage` may use the format appropriate for its task.
- Prefer adding behavior to a skill or sub-agent prompt before changing
  `main.py`; register new runtime components explicitly when needed.

## Triggering (GitHub Actions)

The agent is driven by a workflow in the target repo
(`.github/workflows/issuelens.yml`):

1. Mint a GitHub App installation token with `actions/create-github-app-token`.
2. Authenticate to the Foundry agent endpoint via **Azure OIDC** (`azure/login`).
3. POST `{ input, github_token, attachments? }` to the agent's invocations
  endpoint.

Triggers: `issues` opened/reopened (label the issue), `schedule` (batch triage),
and `workflow_dispatch`.

## Run & deploy

- **Local:** `pip install -r requirements.txt`, copy `.env.example` → `.env` and
  fill it in, then `python main.py` (serves `/invocations` and `/responses` on
  `:8088`).
- **Deployment approval gate:** never deploy, redeploy, roll back, or otherwise
  publish an agent version to Microsoft Foundry unless the user explicitly
  authorizes that deployment in the current request. Permission to edit, test,
  investigate, or prepare deployment changes does not imply permission to
  deploy them.
- **Deploy to Foundry:** `azd deploy` — see `azure.yaml` (Python hosted agent,
  `codeConfiguration` remote build) and `agent.yaml` (hosted-agent manifest). A
  `Dockerfile` is also provided for a container build.

## Layout

- `main.py` — agent server, session wiring, custom-agent registration
- `agents.md` — global IssueLens identity and current runtime scope, works as orchestrator for sub-agents and skills
- `agents/` — sub-agent prompts (`triage.md`, `find-criticals.md`)
- `skills/` — modular skills (`find-duplicates`, `label-issue`, `assign-issue`,
  `notify`)
- `azure.yaml` / `agent.yaml` / `Dockerfile` — deployment config
- `.github/workflows/issuelens.yml` — the triggering workflow
