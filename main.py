# Copyright (c) Microsoft. All rights reserved.

"""IssueLens — a GitHub issue-triage agent (GitHub Copilot SDK + Foundry).

Triages GitHub issues (finds critical hot/blocking/regression issues), applies
labels, and sends notifications. It runs as a Foundry hosted agent exposing the
invocations protocol.

The invocation payload has exactly two fields:

* ``input`` — the user's task (a free-form text prompt).
* ``github_token`` — a GitHub token used to authenticate the remote GitHub MCP
  server, so the agent reads issues / applies labels as that token's identity.

Model (inference) auth is selected automatically:

* FOUNDRY_PROJECT_ENDPOINT + AZURE_AI_MODEL_DEPLOYMENT_NAME set
      → BYOK Foundry model. Uses AZURE_AI_MODEL_API_KEY when set (key auth),
        otherwise a Managed Identity token via DefaultAzureCredential.
* GITHUB_TOKEN set → GitHub Copilot model.

Notifications are delivered by in-process function tools — ``send-email`` and
``send-teams-notification`` — that POST to Logic App HTTP endpoints
(``MAILING_URL`` / ``PERSONAL_NOTIFICATION_URL``). Each tool is registered only
when its endpoint env var is set.
"""

import asyncio
import json
import logging
import os
import pathlib
import sys

import httpx
from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse


from azure.ai.agentserver.invocations import InvocationAgentServerHost
from copilot import CopilotClient, PermissionHandler, ProviderConfig
from copilot.session import CustomAgentConfig
from copilot.session_events import SessionEventType
from copilot.tools import Tool, ToolInvocation, ToolResult

load_dotenv(override=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = InvocationAgentServerHost()

_client: CopilotClient | None = None
_client_lock = asyncio.Lock()
_project_dir = pathlib.Path(__file__).parent
_agents_dir = _project_dir / "agents"
_skills_dir = str(_project_dir / "skills")
# The agent's configuration root. The Copilot harness discovers all agent
# configuration from here, so new config files can be dropped in without any
# code change:
#   • instructions — AGENTS.md, .github/copilot-instructions.md, *.instructions.md
#   • MCP servers  — .mcp.json, .vscode/mcp.json
#   • skills       — skill directories (see skills/)
#   • hooks        — .github/hooks/
#   • plugins      — plugin directories
_config_dir = str(_project_dir)
_working_dir = (
    os.environ.get("HOME")
    or os.environ.get("USERPROFILE")
    or os.getcwd()
)

# The IssueLens orchestrator delegates issue analysis to the runtime
# Critical Issue Analyst sub-agent, then handles duplicate detection, labeling,
# assignment, and notifications via its preloaded skills.
_ISSUELENS_AGENT: CustomAgentConfig = {
    "name": "issuelens",
    "display_name": "IssueLens Orchestrator",
    "description": (
        "Orchestrates GitHub issue triage, duplicate detection, labeling, "
        "assignment, and notifications."
    ),
    "prompt": (
        "You are IssueLens, the GitHub issue-triage orchestrator. For every "
        "triage request, delegate issue retrieval, criticality analysis, and "
        "JSON report generation to the Critical Issue Analyst sub-agent. Use "
        "the returned report to perform any requested duplicate detection, "
        "labeling, assignment, and notification steps by following the "
        "preloaded find-duplicates, label-issue, assign-issue, and notify "
        "skills. Handle direct duplicate-check requests with the "
        "find-duplicates skill and direct issue-assignment requests with the "
        "assign-issue skill. Use only the GitHub MCP server for GitHub "
        "operations; never use shell commands or the GitHub CLI. Preserve the "
        "analyst's JSON report and place it at the very end of triage "
        "responses. Never open pull requests."
    ),
    "skills": ["find-duplicates", "label-issue", "assign-issue", "notify"],
}


def _load_agent_prompt(filename: str) -> str:
    return (_agents_dir / filename).read_text(encoding="utf-8").strip()


_CRITICAL_ISSUE_ANALYST: CustomAgentConfig = {
    "name": "critical-issue-analyst",
    "display_name": "Critical Issue Analyst",
    "description": (
        "Triages GitHub issues and identifies critical hot, blocking, and "
        "regression issues for structured daily or weekly reports."
    ),
    "prompt": _load_agent_prompt("critical-issue-analyst.md"),
    "infer": True,
}


# ── BYOK helpers ─────────────────────────────────────────────────────────────


def _byok_provider() -> tuple[ProviderConfig | None, str | None]:
    """Return (provider, model) for BYOK mode, or (None, None) for Copilot mode.

    Uses the FOUNDRY_PROJECT_ENDPOINT directly as a project-level OpenAI
    endpoint (e.g. https://<resource>.services.ai.azure.com/api/projects/<proj>/openai/v1).

    Model auth precedence:

    1. ``AZURE_AI_MODEL_API_KEY`` set → key auth (``api-key`` header). No Azure
       identity/RBAC needed; requires the account to allow local (key) auth.
    2. Otherwise → a Managed Identity bearer token via ``DefaultAzureCredential``
       (requires the runtime identity to have model data-plane access).
    """
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    model = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "")
    if not endpoint or not model:
        return None, None

    api_key = os.environ.get("AZURE_AI_MODEL_API_KEY", "")
    if api_key:
        provider = ProviderConfig(
            type="azure",
            base_url=endpoint,
            wire_api="responses",
            api_key=api_key,
        )
        return provider, model

    from azure.identity import DefaultAzureCredential
    token = DefaultAzureCredential().get_token(
        "https://ai.azure.com/.default"
    ).token

    provider = ProviderConfig(
        type="azure",
        base_url=endpoint,
        wire_api="responses",
        bearer_token=token,
    )
    return provider, model


# ── Client & session management ──────────────────────────────────────────────


def _github_mcp_server(gh_token: str) -> dict:
    """Build the remote GitHub MCP server config for the given token.

    Uses the Copilot-hosted GitHub MCP endpoint
    (https://api.githubcopilot.com/mcp/), authenticated with the token supplied
    in the invocation payload. GitHub actions are attributed to that token's
    identity, with its scoped permissions.
    """
    return {
        "type": "http",
        "url": os.environ.get(
            "GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/"),
        "headers": {"Authorization": f"Bearer {gh_token}"},
        "tools": ["*"],
    }


async def _ensure_client() -> CopilotClient:
    """Start the shared Copilot runtime client once (lazy)."""
    global _client
    async with _client_lock:
        if _client is not None:
            return _client

        provider, _ = _byok_provider()
        github_token = os.environ.get("GITHUB_TOKEN")

        # Isolate the runtime's home dir so it never picks up an ambient GitHub
        # identity from a developer's machine login (which would make GitHub
        # writes attributed to that user instead of the App bot). Harmless in the
        # container, essential for deterministic local behavior.
        base_dir = os.environ.get("COPILOT_HOME") or os.path.join(
            _working_dir, ".issuelens-copilot")
        os.makedirs(base_dir, exist_ok=True)

        if provider:
            # BYOK mode: Foundry model via Managed Identity — no token needed.
            # Disable the runtime's logged-in-user GitHub identity so GitHub
            # actions go through our configured MCP server (installation token →
            # App bot), not the machine's logged-in user.
            client = CopilotClient(
                use_logged_in_user=False, base_directory=base_dir)
        elif github_token:
            # Copilot mode: use GitHub token for the model.
            client = CopilotClient(
                github_token=github_token, base_directory=base_dir)
        else:
            raise RuntimeError(
                "Set GITHUB_TOKEN (Copilot model) or "
                "FOUNDRY_PROJECT_ENDPOINT + AZURE_AI_MODEL_DEPLOYMENT_NAME "
                "(BYOK Foundry model)")
        await client.start()
        _client = client
        return _client


def _build_mcp_servers(gh_token: str | None) -> dict:
    """Build the MCP server config for GitHub resource access.

    Notifications are delivered by in-process tools (see ``_notification_tools``),
    not via an MCP server.
    """
    servers: dict = {}
    if gh_token:
        servers["github"] = _github_mcp_server(gh_token)
    else:
        logger.warning(
            "No github_token in the invocation payload; GitHub tools disabled.")
    return servers


def _build_prompt(payload: dict) -> str:
    """Extract the user's task (a free-form text prompt) from the payload."""
    text = payload.get("input")
    return text.strip() if isinstance(text, str) else ""


# ── Notification tools (email / Teams via Logic App HTTP endpoints) ───────────

_MAILING_URL_ENV = "MAILING_URL"
_PERSONAL_NOTIFICATION_URL_ENV = "PERSONAL_NOTIFICATION_URL"
_RECIPIENTS_ENV = "RECIPIENTS"


def _default_recipients() -> list[str]:
    """Parse the configured default email recipients (comma/semicolon-separated)."""
    raw = os.environ.get(_RECIPIENTS_ENV, "")
    return [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]


async def _post_logicapp(url: str, payload: dict) -> ToolResult:
    """POST a JSON payload to a Logic App endpoint and map the result for the LLM."""
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(url, json=payload)
    except Exception as exc:  # network / timeout
        return ToolResult(
            text_result_for_llm=f"Notification failed: {exc}",
            result_type="failure",
            error=str(exc),
        )
    if 200 <= resp.status_code < 300:
        return ToolResult(
            text_result_for_llm=f"Notification sent (HTTP {resp.status_code}).")
    return ToolResult(
        text_result_for_llm=(
            f"Notification failed: HTTP {resp.status_code} {resp.text[:200]}"),
        result_type="failure",
        error=f"HTTP {resp.status_code}",
    )


def _notification_tools() -> list[Tool]:
    """Build in-process notification tools for the endpoints that are configured.

    Each tool is included only when its Logic App URL env var is set, so an
    unconfigured channel simply isn't offered to the model. Payloads match the
    Logic App HTTP triggers used by the send-email / send-personal-notification
    skills.
    """
    tools: list[Tool] = []

    mailing_url = os.environ.get(_MAILING_URL_ENV)
    if mailing_url:
        async def _send_email(inv: ToolInvocation) -> ToolResult:
            args = inv.arguments or {}
            recipients = args.get("recipients") or _default_recipients()
            if not recipients:
                return ToolResult(
                    text_result_for_llm=(
                        "No recipients provided and RECIPIENTS is not configured."),
                    result_type="failure",
                    error="no recipients",
                )
            payload: dict = {
                "title": args.get("title"),
                "body": args.get("body"),
                "recipients": recipients,
            }
            if args.get("timeFrame"):
                payload["timeFrame"] = args["timeFrame"]
            if args.get("workflowRunUrl"):
                payload["workflowRunUrl"] = args["workflowRunUrl"]
            return await _post_logicapp(mailing_url, payload)

        tools.append(Tool(
            name="send-email",
            description=(
                "Send an HTML email (e.g. an issue-triage report) to one or more "
                "recipients via the configured Logic App."),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Email subject line."},
                    "body": {
                        "type": "string",
                        "description": (
                            "Email body as inline-styled HTML (email clients "
                            "don't support external CSS). See the notify skill "
                            "for the template."),
                    },
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Recipient email addresses. If omitted, the "
                            "configured default recipients (RECIPIENTS) are used."),
                    },
                    "timeFrame": {
                        "type": "string",
                        "description": "Optional date/period context for the header (e.g. 'February 2, 2026').",
                    },
                    "workflowRunUrl": {
                        "type": "string",
                        "description": "Optional URL to the workflow run that generated this report.",
                    },
                },
                "required": ["title", "body"],
            },
            handler=_send_email,
        ))

    personal_url = os.environ.get(_PERSONAL_NOTIFICATION_URL_ENV)
    if personal_url:
        async def _send_teams(inv: ToolInvocation) -> ToolResult:
            args = inv.arguments or {}
            payload: dict = {
                "title": args.get("title"),
                "message": args.get("message"),
                "recipient": args.get("recipient"),
            }
            if args.get("workflowRunUrl"):
                payload["workflowRunUrl"] = args["workflowRunUrl"]
            return await _post_logicapp(personal_url, payload)

        tools.append(Tool(
            name="send-teams-notification",
            description=(
                "Send a Teams personal-chat notification (e.g. an issue-triage "
                "summary) to a recipient via the configured Logic App."),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Notification title."},
                    "message": {
                        "type": "string",
                        "description": "Message body in Markdown.",
                    },
                    "recipient": {
                        "type": "string",
                        "description": "Recipient's email address (Teams personal chat).",
                    },
                    "workflowRunUrl": {
                        "type": "string",
                        "description": "Optional URL to the workflow run.",
                    },
                },
                "required": ["title", "message", "recipient"],
            },
            handler=_send_teams,
        ))

    return tools


# Built once at startup from the configured endpoint env vars.
_NOTIFY_TOOLS = _notification_tools()


async def _stream_response(invocation_id: str, payload: dict):
    """Create a fresh session for this invocation and stream its events as SSE.

    A new session is created per request so each run uses a freshly minted GitHub
    App installation token and its own repository context (multi-tenant safe).
    """
    client = await _ensure_client()
    provider, model = _byok_provider()
    mcp_servers = _build_mcp_servers(payload.get("github_token"))
    prompt = _build_prompt(payload)

    if not prompt:
        yield f"data: {json.dumps({'type': 'error', 'message': 'empty task'})}\n\n".encode()
        return

    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all,
        streaming=True,
        working_directory=_working_dir,
        # Load supporting configuration from the project: skills and instruction
        # files (AGENTS.md, .github/copilot-instructions.md). Config
        # auto-discovery, file hooks, and plugin loading are intentionally NOT
        # enabled — in the hosted container the deployed code dir is read-only,
        # and enabling them makes the runtime try to write there (I/O error 30).
        skill_directories=[_skills_dir],
        instruction_directories=[_config_dir],
        provider=provider,
        model=model,
        mcp_servers=mcp_servers or None,
        tools=_NOTIFY_TOOLS or None,
        custom_agents=[_ISSUELENS_AGENT, _CRITICAL_ISSUE_ANALYST],
        agent="issuelens",
    )
    session_id = getattr(session, "session_id", None)

    queue: asyncio.Queue = asyncio.Queue()

    def on_event(event):
        if event.type == SessionEventType.SESSION_IDLE:
            queue.put_nowait(None)
        elif event.type == SessionEventType.SESSION_ERROR:
            queue.put_nowait(RuntimeError(
                getattr(event.data, "message", "error")))
        else:
            queue.put_nowait(event)

    unsubscribe = session.on(on_event)
    try:
        await session.send(prompt)
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                yield f"data: {json.dumps({'type': 'error', 'message': str(item)})}\n\n".encode()
                break
            yield f"data: {json.dumps(item.to_dict())}\n\n".encode()

        yield f"event: done\ndata: {json.dumps({'invocation_id': invocation_id, 'session_id': session_id})}\n\n".encode()
    finally:
        unsubscribe()
        try:
            await session.disconnect()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("session.disconnect() failed", exc_info=True)


@app.invoke_handler
async def handle_invoke(request: Request) -> Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("body is not a JSON object")

        has_input = isinstance(data.get("input"), str) and data["input"].strip()
        if not has_input:
            raise ValueError('provide a non-empty "input"')
        has_token = (
            isinstance(data.get("github_token"), str)
            and data["github_token"].strip()
        )
        if not has_token:
            raise ValueError('provide a "github_token"')
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_request",
                "message": str(exc),
                "example": {
                    "input": (
                        "Triage open issues in owner/repo and label the "
                        "critical ones."
                    ),
                    "github_token": "ghs_...",
                },
            },
        )
    return StreamingResponse(
        _stream_response(request.state.invocation_id, data),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


if __name__ == "__main__":
    has_token = bool(os.environ.get("GITHUB_TOKEN"))
    has_byok = bool(
        os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        and os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    )
    if not has_token and not has_byok:
        sys.exit(
            "Error: Set GITHUB_TOKEN (Copilot model) or "
            "FOUNDRY_PROJECT_ENDPOINT + AZURE_AI_MODEL_DEPLOYMENT_NAME "
            "(BYOK Foundry model)")
    app.run()
