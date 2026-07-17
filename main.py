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

WorkIQ (notifications) tools are provided by the Foundry toolbox, not this code.
"""

import asyncio
import json
import logging
import os
import pathlib
import sys

from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse


from azure.ai.agentserver.invocations import InvocationAgentServerHost
from copilot import CopilotClient, PermissionHandler, ProviderConfig
from copilot.session_events import SessionEventType

load_dotenv(override=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = InvocationAgentServerHost()

_client: CopilotClient | None = None
_client_lock = asyncio.Lock()
_skills_dir = str(pathlib.Path(__file__).parent / "skills")
_working_dir = (
    os.environ.get("HOME")
    or os.environ.get("USERPROFILE")
    or os.getcwd()
)

# The IssueLens triage agent. Domain knowledge lives in the preloaded skills
# (triage, label-issue, notify); the prompt orchestrates them.
_TRIAGE_AGENT = {
    "name": "issuelens",
    "display_name": "IssueLens Triage Agent",
    "description": (
        "Triages GitHub issues: identifies critical (hot/blocking/regression) "
        "issues, applies labels, and sends notifications."
    ),
    "prompt": (
        "You are IssueLens, an experienced developer who triages GitHub issues "
        "for the given repositories. Use the available GitHub tools to read "
        "issues, comments, and labels, and to apply labels. Follow the "
        "preloaded triage, label-issue, and notify skills. When triaging, output "
        "the final JSON summary EXACTLY as specified by the triage skill and "
        "place it at the very end of your response. Never open pull requests."
    ),
    "skills": ["triage", "label-issue", "notify"],
}

# Exclude the built-in shell tools. Otherwise the model may shell out to the
# `gh` CLI for GitHub actions, which uses the host's GitHub identity (the
# machine's logged-in user) instead of our token-scoped GitHub MCP server — so
# writes would be attributed to that user rather than the App bot. Blocking the
# shell forces all GitHub access through the MCP server (installation token →
# App bot) and is also a useful sandboxing measure.
_EXCLUDED_TOOLS = ["builtin:bash", "builtin:powershell", "builtin:shell"]


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

    WorkIQ (notifications) tools are provided by the Foundry toolbox, not here.
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
        skill_directories=[_skills_dir],
        working_directory=_working_dir,
        provider=provider,
        model=model,
        mcp_servers=mcp_servers or None,
        custom_agents=[_TRIAGE_AGENT],
        agent="issuelens",
        excluded_tools=_EXCLUDED_TOOLS,
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
