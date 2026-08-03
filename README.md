**IMPORTANT!** All samples and other resources made available in this GitHub repository ("samples") are designed to assist in accelerating development of agents, solutions, and agent workflows for various scenarios. Review all provided resources and carefully test output behavior in the context of your use case. AI responses may be inaccurate and AI actions should be monitored with human oversight.

# IssueLens — GitHub Issue Triage Agent (Foundry hosted)

A GitHub issue-triage agent built on the [GitHub Copilot SDK](https://pypi.org/project/github-copilot-sdk/) (`CopilotClient`), serving both the [invocations](https://pypi.org/project/azure-ai-agentserver-invocations/) protocol (automation) and the [responses](https://pypi.org/project/azure-ai-agentserver-responses/) protocol (chat). It identifies critical issues (hot / blocking / regression), detects duplicates, applies labels, assigns owners, and sends notifications — deployable as a Foundry hosted agent.

## How It Works

Both protocols run in the same process and share the same orchestrator, skills, and sub-agent; only how the GitHub token is obtained differs.

### Automation — `POST /invocations`

1. Receives a JSON task. The payload has exactly two fields: `input` (the task, a free-form text prompt) and `github_token` (used to authenticate the GitHub MCP server), e.g. `{"input": "Triage open issues in owner/repo", "github_token": "ghs_..."}`.
2. Creates a **fresh Copilot session per request** configured with:
   - the **Foundry model** (BYOK via Managed Identity) or the **GitHub Copilot model** for inference;
   - the remote **GitHub MCP server**, authenticated with the `github_token` from the payload — so the agent reads issues and applies labels as that token's identity;
   - **notification tools provided by the Foundry toolbox**.
3. The preselected `issuelens` orchestrator delegates analysis to the runtime **Critical Issue Analyst** sub-agent registered with the Copilot SDK, then runs the `find-duplicates`, `label-issue`, `assign-issue`, and `notify` skills for requested follow-up actions.
4. Each Copilot `SessionEvent` is streamed back as an SSE `data:` event; a final `event: done` marks the end. Triage runs end with a JSON summary.

### Chat — `POST /responses`

1. Receives an OpenAI Responses request (Foundry playground, Teams, or any Responses client).
2. Reaches GitHub through a **Foundry toolbox** (`TOOLBOX_ENDPOINT`) whose GitHub MCP connection uses **managed OAuth2** — Foundry prompts each caller for consent once and owns their tokens and refresh. The agent authenticates to the toolbox with its own Azure AD token (scope `https://ai.azure.com/.default`) and forwards the per-request `x-agent-foundry-call-id` so the toolbox resolves who is calling.
3. Before each turn it probes the toolbox with `tools/list`. If Foundry returns the JSON-RPC `-32006` / `CONSENT_REQUIRED` error, the agent replies with the consent URL instead of failing.
4. Resumes the conversation's Copilot session each turn (fresh token + call ID per turn, history preserved) and streams the reply as Responses SSE events.

## Environment Variables

### Model (inference) — configure one

| Variable | Required | Description |
|----------|----------|-------------|
| `FOUNDRY_PROJECT_ENDPOINT` | For Foundry model | Azure AI Foundry project endpoint URL. Auto-injected when hosted — only needed locally |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | For Foundry model | Model deployment name (e.g. `gpt-4o`) |
| `GITHUB_TOKEN` | For Copilot model | GitHub fine-grained PAT with **Copilot Requests → Read-only** permission |

If the Foundry variables are set they take precedence over `GITHUB_TOKEN`.

### GitHub resource access

For **invocations**, the agent authenticates the remote GitHub MCP server with the `github_token` supplied in each payload — there is nothing to configure via environment variables.

For **chat**, there is no payload token, so GitHub access goes through a Foundry toolbox with a managed-OAuth2 GitHub connection:

| Variable | Required | Description |
|----------|----------|-------------|
| `TOOLBOX_ENDPOINT` | For chat | Toolbox MCP endpoint, e.g. `https://<account>.services.ai.azure.com/api/projects/<project>/toolboxes/<toolbox>/mcp?api-version=v1`. The `?api-version=v1` query string is required |
| `GITHUB_MCP_URL` | No | GitHub MCP endpoint used by the **invocations** path (default `https://api.githubcopilot.com/mcp/`). Override for GitHub Enterprise |

Create the connection and toolbox once:

```bash
azd ai connection create github-oauth-conn \
  --kind remote-tool \
  --target https://api.githubcopilot.com/mcp \
  --auth-type oauth2 \
  --managed-connector foundrygithubmcp

azd ai toolbox create issuelens-tools --from-file ./toolbox.yaml
azd ai toolbox show issuelens-tools --output json   # copy the MCP endpoint
azd env set TOOLBOX_ENDPOINT "<mcp-endpoint>"
```

The agent calls the toolbox with its own identity, so it needs data-plane access to the Foundry project (`Foundry User` or `Azure AI Developer`). Locally that identity comes from `az login`; when hosted it's the agent's managed identity. Foundry stores and refreshes the per-user GitHub tokens — none are held by this codebase.

### Notifications

WorkIQ (notification) tools are provided by the **Foundry toolbox** and are not configured in this codebase.

## Running Locally

### Prerequisites

- Python 3.10+
- A GitHub fine-grained PAT (`github_pat_` prefix)

Create one at [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new) with **Account permissions → Copilot Requests → Read-only**.

> **Note:** Classic tokens (`ghp_`) are not supported. Use a fine-grained PAT (`github_pat_`), OAuth token (`gho_`), or GitHub App user token (`ghu_`).

### Using `azd`

<details>
<summary><strong>Show steps</strong></summary>

Create a local `.env` file from the sample template and set `GITHUB_TOKEN`:

```bash
cp .env.example .env  # skip if .env already exists
# Edit .env and set GITHUB_TOKEN=github_pat_...
```

The sample loads `.env` automatically when running locally. If you plan to deploy with `azd`, also add the token to your azd environment so it can be injected into the hosted agent:

```bash
azd env set GITHUB_TOKEN="github_pat_..."
```

Next, start the agent locally with the `run` command:

```bash
azd ai agent run
```

The agent starts on `http://localhost:8088/`.

</details>

### Using the Foundry Toolkit VS Code Extension

The [Foundry Toolkit VS Code extension](https://learn.microsoft.com/en-us/azure/foundry/agents/quickstarts/quickstart-hosted-agent?view=foundry&pivots=vscode) has a built-in sample gallery. You can open this sample directly from the extension without cloning the repository, it scaffolds the project into a new workspace, generates `agent.yaml`, `.env`, and `.vscode/tasks.json` + `launch.json` automatically, and configures a one-click **F5** debug experience.

Chat with a running agent using the **Agent Inspector**:

1. Start the agent locally first using **Using `azd`** or **Manual setup** above. The agent listens on `http://localhost:8088/`.
2. Open the Command Palette (`Ctrl+Shift+P`) and run **Foundry Toolkit: Open Agent Inspector**.
3. The Inspector auto-connects to the running agent. Send messages to chat with the agent and watch the streamed responses.

### Manual setup

```bash
pip install -r requirements.txt
cp .env.example .env  # skip if .env already exists
# Edit .env and set GITHUB_TOKEN=github_pat_...
python main.py
```

The agent starts on `http://localhost:8088/`.

## Invoke with azd

<details>
<summary><strong>Show steps</strong></summary>

### Local

**Bash:**
```bash
azd ai agent invoke --local '{"input": "Triage open issues in microsoft/vscode-java-pack and label the critical ones", "github_token": "ghs_..."}'
```

**PowerShell:**
```powershell
azd ai agent invoke --local '{\"input\": \"Triage open issues in microsoft/vscode-java-pack and label the critical ones\", \"github_token\": \"ghs_...\"}'
```

### Test with curl

```bash
# Triage a repository (find critical issues) and notify
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Triage open issues updated in the last 24h in owner/repo, then send the report", "github_token": "ghs_..."}'

# Label a single issue
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Label issue owner/repo#123", "github_token": "ghs_..."}'

# Assign a single issue using area ownership and historical patterns
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Assign issue owner/repo#123 to the right owner", "github_token": "ghs_..."}'

# Find duplicate or related reports for a single issue
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Find duplicates for issue owner/repo#123", "github_token": "ghs_..."}'

# Free-form instruction
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Summarize open issues in owner/repo", "github_token": "ghs_..."}'

# Chat (responses protocol) — no token in the body; GitHub access comes from
# the Foundry toolbox, which prompts for OAuth consent on first use.
curl -N -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "Find duplicates for issue owner/repo#123", "stream": true}'
```

### Chat from a terminal

`chat.py` is a small REPL for the chat protocol — it chains `previous_response_id`
so turns stay in one conversation:

```bash
python chat.py                                   # interactive
python chat.py "Triage open issues in owner/repo"  # one-shot
```

Type `new` to start a fresh conversation, `exit` to quit.

### Request fields

Invocations (`POST /invocations`):

| Field | Required | Description |
|-------|----------|-------------|
| `input` | Yes | The task — a free-form text prompt describing what to triage, label, or report |
| `github_token` | Yes | GitHub token used to authenticate the remote GitHub MCP server (e.g. minted by a GitHub Actions workflow) |

Chat (`POST /responses`) takes a standard OpenAI Responses body; GitHub auth is handled by the Foundry toolbox's managed-OAuth2 connection.

### SSE Event Format

Each Copilot SDK event is streamed via `event.to_dict()`:

```
data: {"type": "assistant.message_delta", "data": {"delta_content": "Python is"}}\n\n
data: {"type": "assistant.message_delta", "data": {"delta_content": " a programming"}}\n\n
...
event: done
data: {"invocation_id": "...", "session_id": "..."}
```

</details>

## Triggering with GitHub Actions

The recommended way to trigger the agent is a **GitHub Actions workflow in the target repository**. The workflow authenticates as the IssueLens GitHub App with [`actions/create-github-app-token`](https://github.com/actions/create-github-app-token) and passes the resulting installation token to the agent (via the `github_token` field), so the agent acts on GitHub as the App bot — no PAT, and no App private key on the agent for this path.

- **Event-driven:** `on: issues` (opened/reopened) → `label` mode.
- **Scheduled:** `on: schedule` (cron) → `triage` mode.

Copy [templates/github-actions/issuelens.yml](templates/github-actions/issuelens.yml) into a target repo's `.github/workflows/`, then configure the App client id (variable), App private key (secret), and the agent URL/scope. The template's header comments list every required variable and secret.

> **Alternative (kept as backup):** [webhook_bridge/](webhook_bridge) is a GitHub App **webhook** → Azure Function → queue → agent path (install-and-go, no per-repo files, lowest latency). It's retained as an alternative trigger transport but is not required for the Actions-based setup.

## Using Your Own Foundry Model

To use your own Azure AI Foundry model instead of the Copilot model, set the Foundry variables (no `GITHUB_TOKEN` needed):

```bash
FOUNDRY_PROJECT_ENDPOINT=https://<account>.services.ai.azure.com/api/projects/<project> \
AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-4o \
python main.py
```

Authentication uses Managed Identity via `DefaultAzureCredential`. When deployed as a hosted agent, `FOUNDRY_PROJECT_ENDPOINT` is auto-injected by the platform — you only need to set `AZURE_AI_MODEL_DEPLOYMENT_NAME` in `agent.yaml`.

## Deploying the Agent to Microsoft Foundry

Once you've tested locally, deploy to Microsoft Foundry:

```bash
# Provision Azure resources (skip if already done during local setup)
azd provision

# Build, push, and deploy the agent to Foundry
azd deploy
```

After deploying, invoke the agent running in Foundry:

**Bash:**
```bash
azd ai agent invoke '{"input": "What can you help me with?"}'
```

**PowerShell:**
```powershell
azd ai agent invoke '{\"input\": \"What can you help me with?\"}'
```

To stream logs from the running agent:

```bash
azd ai agent monitor
```

For the full deployment guide, see [Azure AI Foundry hosted agents](https://aka.ms/azdaiagent/docs).

### Deploying with the Foundry Toolkit VS Code Extension

1. Open the Command Palette (`Ctrl+Shift+P`) and run **Foundry Toolkit: Deploy Hosted Agent**. The extension opens a tab-based **Deploy Hosted Agent** wizard and reads `agent.yaml` to auto-populate what it can.
2. If prompted, complete **Foundry Project Setup** to pick the subscription and Foundry project (or create a new one) to deploy to.
3. On the **Basics** tab, configure the core deployment settings:
   - **Deployment Method**: **Code** (upload as a ZIP) or **Container** (Docker image via ACR).
   - For **Code**, pick a packaging option: **Remote** or **Local**.
   - For **Container**, pick a registry option: default ACR, your own ACR, or a prebuilt ACR image.
   - **Hosted Agent Name**: confirm the name to register with the hosting service.
4. On the **Review + Deploy** tab, finalize the runtime and resources:
   - Confirm the auto-detected runtime details (language, entry point, or Dockerfile).
   - Pick a **CPU and Memory** size.
   - Click **Deploy**. Fields are validated inline, and the extension handles the build/upload, agent version creation, and RBAC role assignment.
5. After deployment, invoke the agent in the Agent Playground and stream live logs from the **Logs** tab.

## Sub-agent and skills

The Foundry hosted agent registers both the `issuelens` orchestrator and the `critical-issue-analyst` sub-agent as Copilot SDK `CustomAgentConfig` objects in `main.py`. The analyst prompt is maintained separately and loaded explicitly at startup, so VS Code does not discover it as a workspace custom agent:

```
agents/
└── critical-issue-analyst.md  ← runtime sub-agent prompt

skills/
├── find-duplicates/ ← identify duplicate and related issues via GitHub MCP
├── label-issue/     ← classify and apply labels via GitHub MCP
├── assign-issue/    ← route and assign issues via GitHub MCP
└── notify/          ← send the report via WorkIQ
```

The **Critical Issue Analyst** is available to the parent through runtime sub-agent inference. It uses the request-scoped GitHub MCP server inherited from the session, identifies hot, blocking, and regression issues, and returns the structured JSON report. Issue mutations and notifications remain the responsibility of the parent orchestrator.

Any subdirectory under `skills/` containing a `SKILL.md` file is loaded by the Copilot SDK.

To add your own skill, create a new folder under `skills/` with a `SKILL.md`:

```bash
mkdir skills/my-skill
cat > skills/my-skill/SKILL.md << 'EOF'
---
name: my-skill
description: What this skill does.
---

# My Skill

Instructions for Copilot when this skill is active.
...
```

## Troubleshooting

### Images built on Apple Silicon or other ARM64 machines do not work on our service

**Deploy with `azd deploy`**, which uses ACR remote build and always produces images with the correct architecture.

If you choose to **build locally**, and your machine is **not `linux/amd64`** (for example, an Apple Silicon Mac), the image will **not be compatible with our service**, causing runtime failures.

**Fix for local builds:**

```bash
docker build --platform=linux/amd64 -t image .
```

This forces the image to be built for the required `amd64` architecture.
