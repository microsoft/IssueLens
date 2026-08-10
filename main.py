# Copyright (c) Microsoft. All rights reserved.

"""IssueLens — a GitHub issue-triage agent (GitHub Copilot SDK + Foundry).

Triages GitHub issues (finds critical hot/blocking/regression issues), applies
labels, and sends notifications. It runs as a Foundry hosted agent that serves
two protocols from a single host:

* **invocations** (``POST /invocations``) — automation, e.g. GitHub Actions.
* **responses** (``POST /responses``) — interactive chat (playground, Teams,
  any OpenAI Responses client).

The invocation payload has two required fields and one optional field:

* ``input`` — the user's task (a free-form text prompt).
* ``github_token`` — a GitHub token used to authenticate the remote GitHub MCP
  server, so the agent reads issues / applies labels as that token's identity.
* ``attachments`` — optional inline Copilot ``blob`` attachments containing
    base64-encoded images or files.

Chat has no payload token, so the skill-owned ``github-access`` tool uses the
IssueLens GitHub App. Its bundled skill helper resolves the installation for
each target repository, mints and caches a short-lived token, and performs only
the allowlisted issue-triage operations. Tokens are never returned to the model.
The trusted issue-image loader uses the same protocol-specific GitHub identity
and attaches validated issue-body images to the Copilot turn as vision content.

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
import importlib.util
import json
import logging
import os
import pathlib
import sys
import time

import httpx
from azure.core.credentials import AccessToken
from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse


from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponsesAgentServerHost,
)
from copilot import CopilotClient, PermissionHandler, ProviderConfig
from copilot.session import CustomAgentConfig
from copilot.session_events import (
    AssistantMessageDeltaData,
    SessionEventType,
    SessionIdleData,
)
from copilot.tools import Tool, ToolInvocation, ToolResult
from issue_image_context import issue_image_attachments
from issuelens_config_tool import create_tool as create_issuelens_config_tool
from media_inputs import (
    MAX_ATTACHMENTS,
    MediaInputError,
    invocation_attachments,
    redacted_input_items,
    response_input,
)
from related_github_tool import create_tool as create_related_github_tool

load_dotenv(override=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class IssueLensHost(InvocationAgentServerHost, ResponsesAgentServerHost):
    """One host, both protocols — cooperative init merges each one's routes."""


app = IssueLensHost()

_client: CopilotClient | None = None
_client_lock = asyncio.Lock()
_project_dir = pathlib.Path(__file__).parent
_agents_dir = _project_dir / "agents"
_skills_dir = str(_project_dir / "skills")
_github_app_script = (
    _project_dir / "skills" / "github-access" / "scripts" / "github_app.py"
)
_github_access_tool_script = (
    _project_dir / "skills" / "github-access" / "scripts" / "tool.py"
)


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_github_app = _load_module("issuelens_github_app", _github_app_script)
_github_access_tool = _load_module(
    "issuelens_github_access_tool", _github_access_tool_script
)
_working_dir = (
    os.environ.get("HOME")
    or os.environ.get("USERPROFILE")
    or os.getcwd()
)


def _load_prompt(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8").strip()


# The global agent identity is maintained as a deployable system prompt instead
# of being embedded in application wiring.
_ISSUELENS_AGENT: CustomAgentConfig = {
    "name": "issuelens",
    "display_name": "IssueLens",
    "description": (
        "Triages GitHub repository issues and performs requested duplicate, "
        "labeling, assignment, and notification follow-up actions."
    ),
    "prompt": _load_prompt(_project_dir / "agents.md"),
}


_TRIAGE_AGENT: CustomAgentConfig = {
    "name": "triage",
    "display_name": "Triage",
    "description": (
        "Analyzes a target GitHub issue and returns structured classification, "
        "duplicate, label, priority, and assignee recommendations."
    ),
    "prompt": _load_prompt(_agents_dir / "triage.md"),
    "skills": [
        "github-access",
        "issuelens-config",
        "find-duplicates",
        "label-issue",
        "assign-issue",
        "notify",
    ],
    "infer": True,
}


_FIND_CRITICALS_AGENT: CustomAgentConfig = {
    "name": "find-criticals",
    "display_name": "Find Criticals",
    "description": (
        "Scans GitHub issues and returns a structured report of hot, blocking, "
        "and regression issues."
    ),
    "prompt": _load_prompt(_agents_dir / "find-criticals.md"),
    "skills": ["github-access", "issuelens-config"],
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
            "No github_token available; GitHub tools disabled.")
    return servers


def _session_options(
    mcp_servers: dict,
    runtime_tools: list[Tool] | None = None,
) -> dict:
    """Common ``create_session`` keyword arguments for both protocols."""
    provider, model = _byok_provider()
    return {
        "on_permission_request": PermissionHandler.approve_all,
        "streaming": True,
        "working_directory": _working_dir,
        # Skills and all agent prompts are loaded explicitly so local and hosted
        # behavior is identical.
        "skill_directories": [_skills_dir],
        "provider": provider,
        "model": model,
        "mcp_servers": mcp_servers or None,
        "tools": (
            _RUNTIME_TOOLS if runtime_tools is None else runtime_tools
        ) or None,
        "custom_agents": [
            _ISSUELENS_AGENT,
            _TRIAGE_AGENT,
            _FIND_CRITICALS_AGENT,
        ],
        "agent": "issuelens",
    }


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


# Built once at startup from configured endpoint and App credential variables.
_NOTIFICATION_TOOLS = _notification_tools()
try:
    _GITHUB_APP_PROVIDER = _github_app.GitHubAppTokenProvider.from_environment()
except _github_app.GitHubAppError:
    _GITHUB_APP_PROVIDER = None

_GITHUB_APP_CLIENT = (
    _github_app.GitHubAppClient(_GITHUB_APP_PROVIDER)
    if _GITHUB_APP_PROVIDER
    else None
)
_GITHUB_ACCESS_TOOL = (
    _github_access_tool.create_tool(_github_app, client=_GITHUB_APP_CLIENT)
    if _GITHUB_APP_CLIENT
    else None
)
_ISSUELENS_CONFIG_TOOL = (
    create_issuelens_config_tool(_GITHUB_APP_CLIENT)
    if _GITHUB_APP_CLIENT
    else None
)
_RELATED_GITHUB_TOOL = (
    create_related_github_tool()
    if _GITHUB_APP_CLIENT
    else None
)
if _GITHUB_ACCESS_TOOL is None:
    logger.info("GitHub App chat tool is not configured")
_RUNTIME_TOOLS = [
    *_NOTIFICATION_TOOLS,
    *([_GITHUB_ACCESS_TOOL] if _GITHUB_ACCESS_TOOL else []),
    *([_ISSUELENS_CONFIG_TOOL] if _ISSUELENS_CONFIG_TOOL else []),
    *([_RELATED_GITHUB_TOOL] if _RELATED_GITHUB_TOOL else []),
]


async def _stream_response(invocation_id: str, payload: dict):
    """Create a fresh session for this invocation and stream its events as SSE.

    A new session is created per request so its GitHub MCP server and issue-image
    tool use only that request's token and repository context.
    """
    client = await _ensure_client()
    mcp_servers = _build_mcp_servers(payload.get("github_token"))
    prompt = _build_prompt(payload)
    attachments = payload.get("_copilot_attachments") or []

    if not prompt:
        yield f"data: {json.dumps({'type': 'error', 'message': 'empty task'})}\n\n".encode()
        return

    request_github_client = _github_app.GitHubAppClient(
        _github_app.RequestTokenProvider(payload["github_token"])
    )
    request_config_tool = create_issuelens_config_tool(request_github_client)
    request_related_tool = create_related_github_tool()
    try:
        issue_attachments = await issue_image_attachments(
            prompt,
            request_github_client,
            maximum_images=max(0, MAX_ATTACHMENTS - len(attachments)),
        )
    except _github_app.GitHubAppError:
        logger.info("Invocation issue-body images could not be loaded")
        issue_attachments = []
    attachments = [*attachments, *issue_attachments]
    session = await client.create_session(
        **_session_options(
            mcp_servers,
            [*_NOTIFICATION_TOOLS, request_config_tool, request_related_tool],
        )
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
        await session.send(prompt, attachments=attachments or None)
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
        data["_copilot_attachments"] = invocation_attachments(
            data.get("attachments")
        )
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
                    "attachments": [
                        {
                            "type": "blob",
                            "data": "<base64>",
                            "mimeType": "image/png",
                            "displayName": "screenshot.png",
                        }
                    ],
                },
            },
        )
    return StreamingResponse(
        _stream_response(request.state.invocation_id, data),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ── Chat (responses protocol) ────────────────────────────────────────────────
#
# Chat uses the skill-owned GitHub tools registered above for GitHub.
# A Foundry toolbox MCP server supplies only non-GitHub capabilities.

_TOOLBOX_ENDPOINT_ENV = "TOOLBOX_ENDPOINT"
_TOOLBOX_SCOPE = "https://ai.azure.com/.default"
_CALL_ID_HEADER = "x-agent-foundry-call-id"
_TOKEN_REFRESH_MARGIN_SECONDS = 300

_ANONYMOUS_CONVERSATION = "anonymous"
_MAX_CHAT_SESSIONS = 500

_GREETING = (
    "I'm IssueLens. Ask me to triage a repository's issues, find duplicates, "
    "label an issue, assign an owner, or send a triage report."
)
_ATTACHMENT_ONLY_PROMPT = "Analyze the attached content for this issue-triage task."
_GITHUB_APP_UNCONFIGURED = (
    "GitHub access for chat isn't configured. Set `GITHUB_APP_ID` and "
    "`GITHUB_APP_PRIVATE_KEY_SECRET_URI` (hosted) or "
    "`GITHUB_APP_PRIVATE_KEY_PATH` (local)."
)

# Copilot session id per conversation, so chat stays multi-turn.
_chat_session_ids: dict[str, str] = {}

_toolbox_credential = None
_toolbox_token: AccessToken | None = None


def _toolbox_bearer() -> str:
    """Return a cached Azure AD token for the Foundry toolbox."""
    global _toolbox_credential, _toolbox_token
    stale = (
        _toolbox_token is None
        or _toolbox_token.expires_on - time.time()
        < _TOKEN_REFRESH_MARGIN_SECONDS
    )
    if stale:
        from azure.identity import DefaultAzureCredential

        if _toolbox_credential is None:
            _toolbox_credential = DefaultAzureCredential()
        _toolbox_token = _toolbox_credential.get_token(_TOOLBOX_SCOPE)
    token = _toolbox_token
    if token is None:  # pragma: no cover - defensive narrowing
        raise RuntimeError("Could not acquire a Foundry toolbox token")
    return token.token


def _toolbox_mcp_server(endpoint: str, call_id: str | None) -> dict:
    """Build the authenticated toolbox MCP configuration for one chat turn."""
    headers = {"Authorization": f"Bearer {_toolbox_bearer()}"}
    if call_id:
        headers[_CALL_ID_HEADER] = call_id
    return {
        "type": "http",
        "url": endpoint,
        "headers": headers,
        "tools": ["*"],
    }


async def _close_session(session) -> None:
    try:
        await session.disconnect()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.debug("session.disconnect() failed", exc_info=True)


async def _chat_session(conversation: str, mcp_servers: dict):
    """Open this conversation's Copilot session, resuming it when one exists.

    A session is resumed per conversation so chat history is preserved.
    """
    client = await _ensure_client()
    options = _session_options(mcp_servers)

    session_id = _chat_session_ids.get(conversation)
    if session_id:
        try:
            return await client.resume_session(session_id, **options)
        except Exception:
            logger.info(
                "Could not resume session %s; starting a new one", session_id)

    session = await client.create_session(**options)
    while len(_chat_session_ids) >= _MAX_CHAT_SESSIONS:
        _chat_session_ids.pop(next(iter(_chat_session_ids)))
    _chat_session_ids[conversation] = session.session_id
    return session


@app.response_handler
async def handle_chat(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    """Chat entry point — streams the agent's reply as Responses SSE events."""
    stream = ResponseEventStream(
        response_id=context.response_id, request=request)
    yield stream.emit_created()
    yield stream.emit_in_progress()

    input_items = await context.get_input_items(resolve_references=True)
    logger.info(
        "responses.user_input_items=%s",
        json.dumps(
            redacted_input_items(input_items),
            ensure_ascii=False,
            default=str,
        ),
    )

    try:
        prompt, attachments = response_input(input_items)
    except MediaInputError as exc:
        logger.info("responses.media_input_rejected=%s", exc)
        for event in stream.output_item_message(f"Unsupported attachment: {exc}"):
            yield event
        yield stream.emit_completed()
        return

    prompt = prompt.strip()
    logger.info("responses.user_input_text=%s", json.dumps(prompt, ensure_ascii=False))
    if not prompt:
        if attachments:
            prompt = _ATTACHMENT_ONLY_PROMPT
        else:
            for event in stream.output_item_message(_GREETING):
                yield event
            yield stream.emit_completed()
            return

    if not any(tool.name == "github-access" for tool in _RUNTIME_TOOLS):
        for event in stream.output_item_message(_GITHUB_APP_UNCONFIGURED):
            yield event
        yield stream.emit_completed()
        return

    if _GITHUB_APP_CLIENT:
        try:
            issue_attachments = await issue_image_attachments(
                prompt,
                _GITHUB_APP_CLIENT,
                maximum_images=max(0, MAX_ATTACHMENTS - len(attachments)),
            )
        except _github_app.GitHubAppError:
            logger.info("Chat issue-body images could not be loaded")
            issue_attachments = []
        attachments = [*attachments, *issue_attachments]

    conversation = (
        context.conversation_id
        or context.platform_context.user_id_key
        or _ANONYMOUS_CONVERSATION
    )
    toolbox_endpoint = os.environ.get(_TOOLBOX_ENDPOINT_ENV, "").strip()
    mcp_servers = {}
    if toolbox_endpoint:
        mcp_servers["toolbox"] = _toolbox_mcp_server(
            toolbox_endpoint, context.platform_context.call_id
        )
    session = await _chat_session(conversation, mcp_servers)

    queue: asyncio.Queue = asyncio.Queue()

    def on_event(event):
        data = event.data
        if isinstance(data, AssistantMessageDeltaData):
            queue.put_nowait(data.delta_content or "")
        elif isinstance(data, SessionIdleData):
            queue.put_nowait(None)
        elif event.type == SessionEventType.SESSION_ERROR:
            queue.put_nowait(RuntimeError(getattr(data, "message", "error")))

    unsubscribe = session.on(on_event)
    message = stream.add_output_item_message()
    yield message.emit_added()
    text = message.add_text_content()
    yield text.emit_added()
    try:
        await session.send(prompt, attachments=attachments or None)
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            if item:
                yield text.emit_delta(item)
    finally:
        unsubscribe()
        await _close_session(session)

    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


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
