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
  existing labels and the validated `labeling` instruction domain.
- **Auto-assignment** — routes issues to individual owners using repository area
  mappings and historical assignment patterns.
- **Notifications** — sends triage reports through Logic App-backed email and
  Teams notification tools.
- **App-scoped GitHub access** — both protocols use the bundled stdio MCP
  server. It resolves the App installation for each explicit repository and
  mints repository- and permission-scoped tokens, so writes are always
  attributed to the App bot rather than an ambient user identity.

## Architecture

- **`main.py`** — the Foundry hosted-agent server. A single host class
  (`IssueLensHost(InvocationAgentServerHost, ResponsesAgentServerHost)`) serves
  two protocols in one process:
  - **`POST /invocations`** — automation (GitHub Actions). Each request opens a
    fresh Copilot session and streams session events back as SSE.
    **Invocation payload:** one required field and one optional field —
    `{ "input": "<free-form task>", "attachments": [] }`. `input` is the task;
    `attachments` contains validated inline Copilot `blob` attachments.
  - **`POST /responses`** — chat (playground, Teams, any Responses client).
    The conversation's Copilot session is resumed each turn.
  - **Session-owned GitHub MCP** — every Copilot session starts the bundled
    `github_app_mcp` stdio process with the App ID and Key Vault secret URI. The
    process loads the private key lazily, resolves installations, and caches
    short-lived tokens only in memory for its process/session lifetime. A token
    is restricted to one repository and the minimum tool permission set.
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
- **Skills** (`skills/`): `issuelens-config` (validated repository policy),
  `find-duplicates`, `label-issue`, `assign-issue`, and `notify`.
- **Media inputs** — `media_inputs.py` normalizes Responses `input_image` and
  `input_file` content and invocation `blob` attachments into Copilot session
  attachments. Only inline base64 content is accepted; remote URLs, file IDs,
  and request-supplied server paths are rejected.
- **GitHub access** — both protocols use only the bundled GitHub App stdio MCP
  tools for model-facing GitHub reads and writes. Every tool requires an
  explicit `owner/repository`; successful App installation resolution is the
  repository authorization boundary. The constrained `issuelens-config` tool
  and host image loader create separate request-local, read-only App clients.
  Related repositories named by duplicate instructions use the same MCP tools
  and must be included in an App installation.
- **Runtime configuration** — `main.py` explicitly loads `agents.md`,
  both sub-agent prompts under `agents/`, and the skill directories. Explicit
  loading keeps local and hosted behavior identical without enabling config
  discovery in the read-only hosted code directory.

## Conventions

- **GitHub access has one model-facing boundary** — use only the bundled
  IssueLens GitHub MCP tools for both protocols. The constrained
  `issuelens-config` host tool returns one validated policy domain. Never shell
  out to `gh` / `bash` / `powershell`, call GitHub over direct HTTP, use a
  Foundry GitHub toolbox connection, or expose App credentials. See `agents.md`.
- Only `find-criticals` is required to return JSON, which IssueLens preserves at
  the end of its response. `triage` may use the format appropriate for its task.
- Prefer adding behavior to a skill or sub-agent prompt before changing
  `main.py`; register new runtime components explicitly when needed.

## Triggering (GitHub Actions)

The agent is driven by a workflow in the target repo
(`.github/workflows/issue-triage.yml`):

1. Authenticate to the Foundry agent endpoint via **Azure OIDC** (`azure/login`).
2. POST `{ input, attachments? }` to the agent's invocations endpoint. The
  hosted agent owns the App credentials; target repositories do not store the
  App private key or mint tokens.

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
- `github_app_mcp/` — bundled GitHub App stdio MCP server and isolated tests
- `agents.md` — global IssueLens identity and current runtime scope, works as orchestrator for sub-agents and skills
- `agents/` — sub-agent prompts (`triage.md`, `find-criticals.md`)
- `skills/` — modular skills (`issuelens-config`, `find-duplicates`,
  `label-issue`, `assign-issue`, `notify`)
- `azure.yaml` / `agent.yaml` / `Dockerfile` — deployment config
- `.github/workflows/issue-triage.yml` — the triggering workflow
