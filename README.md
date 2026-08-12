**IMPORTANT!** All samples and other resources made available in this GitHub repository ("samples") are designed to assist in accelerating development of agents, solutions, and agent workflows for various scenarios. Review all provided resources and carefully test output behavior in the context of your use case. AI responses may be inaccurate and AI actions should be monitored with human oversight.

# IssueLens — GitHub Issue Triage Agent (Foundry hosted)

A GitHub issue-triage agent built on the [GitHub Copilot SDK](https://pypi.org/project/github-copilot-sdk/) (`CopilotClient`), serving both the [invocations](https://pypi.org/project/azure-ai-agentserver-invocations/) protocol (automation) and the [responses](https://pypi.org/project/azure-ai-agentserver-responses/) protocol (chat). It identifies critical issues (hot / blocking / regression), detects duplicates, applies labels, assigns owners, and sends notifications — deployable as a Foundry hosted agent.

## How It Works

Both protocols run in the same process and share the same orchestrator, skills,
two sub-agents, and bundled GitHub App stdio MCP server.

### Automation — `POST /invocations`

1. Receives a JSON task. The payload requires `input` (the task, a free-form
   text prompt), with optional inline `attachments`, e.g.
   `{"input": "Triage open issues in owner/repo"}`.
2. Creates a **fresh Copilot session per request** configured with:
   - the **Foundry model** (BYOK via Managed Identity) or the **GitHub Copilot model** for inference;
   - the bundled **GitHub App stdio MCP server**, whose process and token cache
     belong only to that Copilot session;
   - the constrained in-process `issuelens-config` tool, backed by a separate
     request-local read-only App client;
   - in-process notification tools when their Logic App endpoints are configured.
3. The preselected `issuelens` agent gets its global identity and orchestration rules from `agents.md`. It routes issue-level work to `triage` and critical-issue scans to `find-criticals`. The `triage` sub-agent runs the `find-duplicates`, `label-issue`, `assign-issue`, and `notify` skills for requested follow-up actions.
4. Each Copilot `SessionEvent` is streamed back as an SSE `data:` event; a final `event: done` marks the end. Critical-issue scans end with a JSON report.

### Chat — `POST /responses`

1. Receives an OpenAI Responses request (Foundry playground, Teams, or any Responses client), including inline `input_image` and `input_file` content.
2. Starts the bundled GitHub App stdio MCP server for the Copilot session. It
  resolves the installation for each `owner/repository`, mints tokens limited
  to that repository and the minimum required permissions, and never returns
  credentials to the model.
3. Uses a request-local read-only App client for issue-body images and the
  constrained `issuelens-config` tool.
4. Attaches the Foundry toolbox for non-GitHub capabilities such as notifications. The toolbox must not contain a GitHub MCP connection.
5. Performs only the bundled issue-triage operations: repository/file reads,
   issue and comment reads/searches, label reads/additions, assignee updates,
   and explicitly requested issue comments.
6. Resumes the conversation's Copilot session each turn and streams the reply as Responses SSE events.

## Environment Variables

### Model (inference) — configure one

| Variable | Required | Description |
|----------|----------|-------------|
| `FOUNDRY_PROJECT_ENDPOINT` | For Foundry model | Azure AI Foundry project endpoint URL. Auto-injected when hosted — only needed locally |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | For Foundry model | Model deployment name (e.g. `gpt-4o`) |
| `GITHUB_TOKEN` | For Copilot model | GitHub fine-grained PAT with **Copilot Requests → Read-only** permission |

If the Foundry variables are set they take precedence over `GITHUB_TOKEN`.

### GitHub resource access

Configure the IssueLens GitHub App registration for both protocols. Every tool
names its target as `owner/repository`, and the server resolves the matching App
installation dynamically:

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | Yes | Numeric GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY_SECRET_URI` | Yes | Azure Key Vault secret URI containing the App PEM |

Store the PEM in Key Vault; never place it in `.env`, an azd environment, or a
deployment manifest. Grant the hosted agent's managed identity **Key Vault
Secrets User**, then configure only the App ID and secret URI:

```powershell
# Run directly in your own terminal so the PEM never passes through chat.
az keyvault secret set --vault-name <vault> --name issuelens-github-app-key `
  --file .secrets/issuelens.pem

azd env set GITHUB_APP_ID <app-id>
azd env set GITHUB_APP_PRIVATE_KEY_SECRET_URI `
  "https://<vault>.vault.azure.net/secrets/issuelens-github-app-key"
```

Target repositories and all repositories receiving writes must be included in
an installation of the App. Bounded reads prefer App authentication but fall
back to anonymous access for public repositories when no installation is
available. Private repository reads still require an installation. Each Copilot
session owns one stdio MCP process. That process caches tokens only in memory by
repository and permission set, refreshes them five minutes before expiry, and
discards them when the process exits. Configure the App with **Metadata: Read**,
**Issues: Read and write**, and **Contents: Read**. Tokens and the private key
never enter model context.

## Target Repository Configuration

A target repository may select capability-specific Markdown instructions with
one case-insensitive filename match for `.github/issuelens.yml`. The `.github`
directory and every configured instruction path use their exact repository
casing. See [examples/issuelens.yml](examples/issuelens.yml) and validate files
against [schemas/issuelens.schema.json](schemas/issuelens.schema.json).

```yaml
version: 1
instructions:
  criticality:
    path: .github/issuelens/criticality.md
  duplicate_detection:
    path: .github/issuelens/duplicates.md
  labeling:
    path: .github/issuelens/labels.md
  assignment:
    path: .github/issuelens/assignment.md
  notification_content:
    path: .github/issuelens/notifications.md
```

Every instruction domain is optional:

| Domain | Repository-specific policy it may contain |
|--------|-------------------------------------------|
| `criticality` | Core functions, known workarounds, and additional hot/blocking/regression signals |
| `duplicate_detection` | Canonical issue conventions, exclusions, stricter matching evidence, and related repositories for read-only candidate search |
| `labeling` | Existing-label mappings, priority rubric, and component classification |
| `assignment` | Area owners, keyword/path mappings, routing rules, and default owners |
| `notification_content` | Report title, grouping, emphasis, and presentation only |

Fallback behavior is backward compatible:

- If `.github/issuelens.yml` does not exist, labeling still checks
  `.github/label-instructions.md`, assignment still checks
  `.github/area_owners.md`, `docs/area_owners.md`, then `area_owners.md`, and
  all other capabilities use their built-in behavior.
- If the config exists but omits a domain, that domain uses the same legacy or
  built-in fallback.
- If more than one case variant exists, the YAML is invalid, or a configured
  file is missing or invalid, IssueLens stops that capability and does not
  perform its related write. It does not silently bypass a present but invalid
  configuration.

Configuration is limited to one 16 KB YAML document and 64 KB per UTF-8
Markdown instruction file. Paths must be repository-relative POSIX paths.
Repository policy cannot authorize writes, weaken mandatory evidence or safety
rules, choose notification recipients/channels, or override response formats.
Duplicate instructions may name related repositories. IssueLens accesses them
through the same MCP tools, using anonymous fallback for public repositories
without an App installation, and never uses this scope for writes.

### Foundry toolbox

Set `TOOLBOX_ENDPOINT` to the versioned MCP endpoint for the toolbox containing
non-GitHub chat capabilities. GitHub must remain excluded from this toolbox;
GitHub access is provided only by the bundled stdio MCP server.

### Notifications

Email and Teams notification tools are registered in process when their Logic
App endpoint variables are configured.

## Running Locally

### Prerequisites

- Python 3.12+
- A GitHub fine-grained PAT (`github_pat_` prefix)
- Azure credentials that can read the configured Key Vault secret

Create one at [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new) with **Account permissions → Copilot Requests → Read-only**.

> **Note:** Classic tokens (`ghp_`) are not supported. Use a fine-grained PAT (`github_pat_`), OAuth token (`gho_`), or GitHub App user token (`ghu_`).

### Using `azd`

<details>
<summary><strong>Show steps</strong></summary>

Create a local `.env` file from the sample template. Configure one model backend
plus the GitHub App ID and Key Vault secret URI:

```bash
cp .env.example .env  # skip if .env already exists
# Edit .env and set the model variables plus GITHUB_APP_ID and
# GITHUB_APP_PRIVATE_KEY_SECRET_URI.
```

The sample loads `.env` automatically when running locally. `GITHUB_TOKEN` is
needed only when using the optional GitHub Copilot model for local inference;
Foundry BYOK deployments do not use or inject it.

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
# Edit .env and set the model variables plus GITHUB_APP_ID and
# GITHUB_APP_PRIVATE_KEY_SECRET_URI.
python main.py
```

The agent starts on `http://localhost:8088/`.

## Invoke with azd

<details>
<summary><strong>Show steps</strong></summary>

### Local

**Bash:**
```bash
azd ai agent invoke --local '{"input": "Triage open issues in microsoft/vscode-java-pack and label the critical ones"}'
```

**PowerShell:**
```powershell
azd ai agent invoke --local '{\"input\": \"Triage open issues in microsoft/vscode-java-pack and label the critical ones\"}'
```

### Test with curl

```bash
# Triage a repository (find critical issues) and notify
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Triage open issues updated in the last 24h in owner/repo, then send the report"}'

# Label a single issue
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Label issue owner/repo#123"}'

# Assign a single issue using area ownership and historical patterns
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Assign issue owner/repo#123 to the right owner"}'

# Find duplicate or related reports for a single issue
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Find duplicates for issue owner/repo#123"}'

# Free-form instruction
curl -N -X POST http://localhost:8088/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": "Summarize open issues in owner/repo"}'

# Chat (responses protocol) — no token in the body; the bundled server resolves
# the repository's App installation internally.
curl -N -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "Find duplicates for issue owner/repo#123", "stream": true}'
```

### Image and file inputs

Invocation clients send Copilot `blob` attachments. The `data` field is raw
base64 without a data-URL prefix:

```json
{
  "input": "Use this screenshot while triaging owner/repo#123",
  "attachments": [
    {
      "type": "blob",
      "data": "iVBORw0KGgo...",
      "mimeType": "image/png",
      "displayName": "screenshot.png"
    }
  ]
}
```

Responses clients use standard polymorphic message content. Images use a
base64 data URL; generic files use inline `file_data`:

```json
{
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": [
        {"type": "input_text", "text": "Triage owner/repo#123 using this evidence"},
        {
          "type": "input_image",
          "image_url": "data:image/png;base64,iVBORw0KGgo...",
          "detail": "auto"
        },
        {
          "type": "input_file",
          "filename": "diagnostics.txt",
          "file_data": "data:text/plain;base64,ZXJyb3IgbG9n..."
        }
      ]
    }
  ],
  "stream": true
}
```

Only inline base64 media is accepted. Remote URLs, platform `file_id` values,
and invocation `file` paths are rejected to prevent server-side URL fetching
and arbitrary container-file access. Requests may contain up to 10 attachments,
20 MB each and 50 MB combined. The selected model must support the supplied
image or file MIME type.

Issue images are also loaded automatically during issue-link triage. Before the
agent turn, the trusted host loader resolves explicit GitHub issue URLs and
`owner/repository#number` references, reads each issue body, and adds validated
image bytes as Copilot blob attachments. Clients do not need to add those images
to the invocation payload. It accepts up to 5 PNG, JPEG, GIF, or WebP images,
5 MB each and 15 MB combined. Arbitrary image hosts and unsafe redirects are
rejected, and GitHub credentials are never forwarded to signed storage
redirects.

### Chat from a terminal

`chat.py` is a small REPL for the chat protocol — it chains `previous_response_id`
so turns stay in one conversation:

```bash
python chat.py                                   # interactive
python chat.py "Triage open issues in owner/repo"  # one-shot
python chat.py --attach screenshot.png "Triage owner/repo#123"  # with media
```

Type `new` to start a fresh conversation, `exit` to quit.

### Request fields

Invocations (`POST /invocations`):

| Field | Required | Description |
|-------|----------|-------------|
| `input` | Yes | The task — a free-form text prompt describing what to triage, label, or report |
| `attachments` | No | Inline Copilot `blob` attachments with base64 `data`, `mimeType`, and optional `displayName` |

Chat (`POST /responses`) takes a standard OpenAI Responses body; both protocols
use the same internal GitHub App MCP authentication.

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

The recommended trigger is a **GitHub Actions workflow in the target
repository**. It authenticates only to the Foundry endpoint through Azure OIDC
and sends the task. The hosted agent owns its Key Vault-backed App identity, so
target repositories store no App private key and transmit no GitHub token.

- **Event-driven:** `on: issues` (opened/reopened) → `label` mode.
- **Scheduled:** `on: schedule` (cron) → `triage` mode.

Copy [.github/workflows/issue-triage.yml](.github/workflows/issue-triage.yml)
into a target repository's `.github/workflows/`, then configure the Azure OIDC
identity, agent URL/scope, and notification recipients. Keep repository-specific
policy in `.github/issuelens.yml`; the
workflow filename intentionally differs from the policy filename.

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

The Foundry hosted agent registers the `issuelens` orchestrator and its two sub-agents, `triage` and `find-criticals`, as Copilot SDK `CustomAgentConfig` objects in `main.py`. All prompts are loaded explicitly at startup so their behavior is consistent locally and in the hosted package:

```
agents.md                   ← global IssueLens identity and current scope

agents/
├── triage.md               ← issue-level triage and recommendations
└── find-criticals.md        ← critical-issue scan and JSON report

skills/
├── find-duplicates/ ← identify duplicate and related issues
├── label-issue/     ← classify and apply labels
├── assign-issue/    ← route and assign issues
└── notify/          ← send the report via configured notification tools
github_app_mcp/             ← bundled GitHub App stdio MCP server
```

Both sub-agents are available to IssueLens through runtime inference. `triage`
analyzes target issues and owns requested duplicate, label, assignment, and
notification work. `find-criticals` scans a repository and time scope for hot,
blocking, and regression issues and returns the structured report. Both use the
same GitHub App MCP tools, while `agents.md` keeps the parent IssueLens agent
responsible for selecting and sequencing them.

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
