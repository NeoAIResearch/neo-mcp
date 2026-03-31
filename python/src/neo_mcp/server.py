import asyncio
import contextvars
import json
import os
import re
import time
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server  # used as async context manager
from mcp.types import Tool, TextContent
from mcp import types

# Environment: "staging" → alpha.heyneo.so, anything else → master.heyneo.so (prod)
_NEO_ENV = os.environ.get("NEO_ENV", "prod").lower()
_DEFAULT_API_URL = "https://alpha.heyneo.so" if _NEO_ENV == "staging" else "https://master.heyneo.so"
NEO_API_URL = os.environ.get("NEO_API_URL", _DEFAULT_API_URL)

NEO_SECRET_KEY = os.environ.get("NEO_SECRET_KEY", "") # secret key (sk-v1-...) — sole auth token
NEO_READ_ONLY = os.environ.get("NEO_READ_ONLY", "").lower() == "true"
NEO_DEPLOYMENT_ID = os.environ.get("NEO_DEPLOYMENT_ID", "")  # optional, override auto-discovered sandbox ID
NEO_WORKSPACE_DIR = os.environ.get("NEO_WORKSPACE_DIR", "")  # optional, override CWD (useful in Docker)
NEO_TRANSPORT = os.environ.get("NEO_TRANSPORT", "stdio").lower()  # "stdio" or "http"
NEO_HTTP_PORT = int(os.environ.get("NEO_HTTP_PORT") or os.environ.get("PORT", "8000"))
NEO_HTTP_HOST = os.environ.get("NEO_HTTP_HOST", "0.0.0.0")
# Public base URL used in OAuth discovery payloads (override for local dev)
_BASE_URL = os.environ.get("NEO_PUBLIC_URL", "https://mcpserver.heyneo.com")

_THREAD_ID_FILE = os.path.expanduser("~/.neo/active_thread_id")
_THREAD_WORKSPACES_FILE = os.path.expanduser("~/.neo/daemon/thread-workspaces.json")
_DAEMON_DIR = os.path.expanduser("~/.neo/daemon")
_NPM_STARTUP_LOG = os.path.expanduser("~/.neo/daemon/npm_daemon_start.log")
_PYTHON_STARTUP_LOG = os.path.expanduser("~/.neo/daemon/python_daemon_start.log")
_DAEMON_PORT = 31337
# Tracks deployment_ids we are actively heartbeating so we don't start duplicate tasks.
_active_heartbeats: set[str] = set()

# In-memory poll state: { thread_id: { "status": str, "messages": list|None, "capped": bool } }
# Populated and updated by background asyncio tasks; read by neo_task_status / neo_get_messages.
_active_polls: dict[str, dict] = {}

# CLI auth relay: { state_uuid: { "access_token", "refresh_token", "username", "expires" } }
# Written by /auth/callback, consumed once by /auth/poll/{state}. TTL = 5 min.
_cli_auth_relay: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Thread-id persistence
# ---------------------------------------------------------------------------

def _save_thread_id(thread_id: str) -> None:
    """Persist thread_id so follow-up tools can recover it if the caller loses it."""
    try:
        os.makedirs(os.path.dirname(_THREAD_ID_FILE), exist_ok=True)
        with open(_THREAD_ID_FILE, "w") as f:
            f.write(thread_id)
    except OSError:
        pass


def _save_thread_workspace(thread_id: str, workspace: str) -> None:
    """Persist thread -> workspace mapping for daemon/local file recovery."""
    if not thread_id or not workspace:
        return
    try:
        os.makedirs(os.path.dirname(_THREAD_WORKSPACES_FILE), exist_ok=True)
        try:
            with open(_THREAD_WORKSPACES_FILE, "r") as f:
                workspaces = json.load(f)
                if not isinstance(workspaces, dict):
                    workspaces = {}
        except (OSError, json.JSONDecodeError):
            workspaces = {}
        workspaces[thread_id] = workspace
        with open(_THREAD_WORKSPACES_FILE, "w") as f:
            json.dump(workspaces, f, indent=2)
    except OSError:
        pass


def _load_thread_id() -> str:
    """Return the last saved thread_id, or empty string if none."""
    try:
        with open(_THREAD_ID_FILE, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def _resolve_thread_id(arguments: dict) -> tuple[str, bool]:
    """Return (thread_id, was_recovered).

    Uses the caller-supplied value first; falls back to the persisted value.
    """
    tid = arguments.get("thread_id", "").strip()
    if tid and tid != "unknown":
        return tid, False
    stored = _load_thread_id()
    if stored:
        return stored, True
    return "", False


# ---------------------------------------------------------------------------
# Sandbox / deployment ID discovery and creation
# ---------------------------------------------------------------------------

def _vscode_daemon_deployment_id() -> str:
    """Return the most recent sandboxId written to daemon.log by the Python daemon.

    NOTE: In production the VS Code/Cursor extension does NOT write {"sandboxId": ...}
    entries to daemon.log (file logging is disabled for the production backend URL).
    Only the Python daemon writes these entries (with "source": "python-daemon").

    This function is kept as a utility for discovering any previously-run Python
    daemon's deployment ID. It must NOT be used as a proxy for "is VS Code extension
    running?" — use _register_with_daemon() for that (checks localhost:31337).
    """
    for log_name in ("daemon.log", "daemon.log.1"):
        log_path = os.path.expanduser(f"~/.neo/daemon/{log_name}")
        try:
            with open(log_path, "r", errors="ignore") as f:
                lines = f.readlines()
            for line in reversed(lines):
                m = re.search(r'"sandboxId"\s*:\s*"([a-f0-9\-]{36})"', line)
                if m:
                    return m.group(1)
        except OSError:
            pass
    return ""


def _discover_sandbox_id() -> str:
    """Find the active deployment ID from the VS Code/Cursor extension daemon.

    Sources (in priority order):
    1. daemon.log — sandboxId entries written by the extension or standalone setup
    2. standalone_deployment_id — UUID persisted by the Python daemon on first run
    """
    for log_name in ("daemon.log", "daemon.log.1"):
        log_path = os.path.expanduser(f"~/.neo/daemon/{log_name}")
        try:
            with open(log_path, "r", errors="ignore") as f:
                lines = f.readlines()
            best = ""
            for line in lines:
                m = re.search(r'"sandboxId"\s*:\s*"([a-f0-9\-]{36})"', line)
                if m:
                    best = m.group(1)
            if best:
                return best
        except OSError:
            pass

    # Check standalone_deployment_id — written by the Python daemon on first run,
    # before daemon.log. This is the same file the Python daemon uses to persist its UUID.
    standalone_path = os.path.expanduser("~/.neo/daemon/standalone_deployment_id")
    try:
        uid = open(standalone_path).read().strip()
        if uid and re.match(r'^[a-f0-9\-]{36}$', uid):
            return uid
    except OSError:
        pass

    return ""


def _get_deployment_id() -> str:
    """Return deployment ID.

    Priority:
    1. Per-request X-Neo-Deployment-Id header (HTTP mode — set by context var)
    2. NEO_DEPLOYMENT_ID env var
    3. Derived from API key — primary path for daemon routing
    """
    if dep := _ctx_deployment_id.get() or NEO_DEPLOYMENT_ID:
        return dep
    sk = _ctx_secret_key.get() or NEO_SECRET_KEY
    if sk:
        return _derive_deployment_id(sk)
    return ""


async def _heartbeat_loop(deployment_id: str) -> None:
    """Send a heartbeat to the daemon every 60 s to keep the deployment alive.

    The daemon evicts deployments with no heartbeat for > 5 minutes.
    Mirrors start-daemon.sh step 11 (background heartbeat sender).
    """
    token_path = os.path.expanduser("~/.neo/daemon/daemon.token")
    while True:
        await asyncio.sleep(60)
        try:
            with open(token_path) as f:
                token = f.read().strip()
        except OSError:
            break  # Daemon gone — stop heartbeating
        try:
            async with httpx.AsyncClient(timeout=3.0) as dc:
                await dc.post(
                    f"http://127.0.0.1:{_DAEMON_PORT}/heartbeat",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"deploymentId": deployment_id},
                )
        except Exception:
            pass  # Non-fatal; retry next interval


async def _register_with_daemon(deployment_id: str, secret_key: str, workspace: str = "") -> bool:
    """Register deployment_id with the local daemon and start a heartbeat task.

    Mirrors start-daemon.sh steps 8–11:
      - reads daemon.token for IPC auth
      - POST /register with deploymentId + workspaceFolder + authToken
      - launches _heartbeat_loop background task if not already running

    Returns True if registration succeeded, False if daemon is not running/reachable.
    """
    token_path = os.path.expanduser("~/.neo/daemon/daemon.token")
    try:
        with open(token_path) as f:
            token = f.read().strip()
        if not token:
            return False
    except OSError:
        return False

    try:
        async with httpx.AsyncClient(timeout=3.0) as dc:
            resp = await dc.post(
                f"http://127.0.0.1:{_DAEMON_PORT}/register",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "deploymentId": deployment_id,
                    "workspaceFolder": workspace or _server_cwd,
                    "authToken": secret_key,
                },
            )
            if resp.status_code != 200:
                return False
    except Exception:
        return False

    if deployment_id not in _active_heartbeats:
        _active_heartbeats.add(deployment_id)
        asyncio.create_task(_heartbeat_loop(deployment_id))

    return True


def _derive_deployment_id(secret_key: str) -> str:
    """Derive a stable, deterministic deployment UUID from the API key.

    This is the primary UUID strategy for both the hosted server and local
    single-user setups. It mirrors what VS Code's StateManager.getDeploymentId()
    does (persist once, reuse always) — but without files, making it safe for
    multi-user hosted servers where every user has a different API key.

    Properties:
    - Same key → same UUID on every call (stable across restarts)
    - Different keys → different UUIDs (per-user isolation on hosted server)
    - No files or headers needed — works purely from the API key
    """
    import hashlib
    import uuid as _uuid
    # SHA-256 of the key, take first 16 bytes → UUID
    digest = hashlib.sha256(secret_key.encode()).digest()[:16]
    return str(_uuid.UUID(bytes=digest, version=5))


def _derive_local_daemon_deployment_id(secret_key: str) -> str:
    """Derive a stdio-local daemon UUID that won't collide with extension/key UUIDs."""
    import hashlib
    import uuid as _uuid
    digest = hashlib.sha256((secret_key + "::neo-local-daemon-v1").encode()).digest()[:16]
    return str(_uuid.UUID(bytes=digest, version=5))


def _get_or_create_persistent_deployment_id(secret_key: str = "") -> str:
    """Return the best available persistent deployment UUID.

    Priority:
    1. UUID derived from API key (deterministic, per-user, no files needed) —
       used whenever secret_key is available, handles both hosted and local modes
    2. Existing standalone_deployment_id file — for backwards compatibility when
       no key is provided (edge case: tools called outside normal submit flow)
    3. Generate a random UUID as last resort and persist it
    """
    if secret_key:
        return _derive_deployment_id(secret_key)

    # Fallback: file-based UUID (backwards compat, local single-user setups)
    standalone_path = os.path.expanduser("~/.neo/daemon/standalone_deployment_id")
    try:
        uid = open(standalone_path).read().strip()
        if uid and re.match(r'^[a-f0-9\-]{36}$', uid):
            return uid
    except OSError:
        pass
    import uuid as _uuid
    uid = str(_uuid.uuid4())
    try:
        os.makedirs(os.path.expanduser("~/.neo/daemon"), exist_ok=True)
        open(standalone_path, "w").write(uid)
    except OSError:
        pass
    return uid


async def _auto_start_npm_daemon(secret_key: str, deployment_id: str = "", workspace: str = "") -> bool:
    """Start npx neo-mcp-daemon as a detached background process.

    Called automatically by neo_submit_task when no local daemon is available.
    """
    import shutil
    import subprocess

    npx_bin = shutil.which("npx")
    if not npx_bin:
        return False

    cmd = [npx_bin, "--yes", "neo-mcp-daemon", workspace or _server_cwd]
    if deployment_id:
        cmd += ["--deployment-id", deployment_id]

    env = os.environ.copy()
    env["NEO_SECRET_KEY"] = secret_key

    # Avoid spawning duplicates if the target deployment is already alive.
    if _npm_daemon_running(deployment_id):
        return True

    log_fp = None
    try:
        os.makedirs(_DAEMON_DIR, exist_ok=True)
        log_fp = open(_NPM_STARTUP_LOG, "ab")
        subprocess.Popen(
            cmd, env=env,
            stdout=log_fp, stderr=log_fp,
            start_new_session=True,
        )
    except Exception:
        return False
    finally:
        try:
            log_fp.close()
        except Exception:
            pass

    # First npx run may need package download; allow enough time.
    for _ in range(60):
        await asyncio.sleep(0.5)
        if _npm_daemon_running(deployment_id):
            return True

    return False


async def _auto_start_python_daemon(secret_key: str, deployment_id: str = "", workspace: str = "") -> bool:
    """Start `neo-mcp daemon` as a detached background process."""
    import shutil
    import subprocess

    neo_mcp_bin = shutil.which("neo-mcp")
    if not neo_mcp_bin:
        return False

    cmd = [neo_mcp_bin, "daemon", workspace or _server_cwd]
    if deployment_id:
        cmd += ["--deployment-id", deployment_id]

    env = os.environ.copy()
    env["NEO_SECRET_KEY"] = secret_key

    if _python_daemon_running(deployment_id):
        return True

    log_fp = None
    try:
        os.makedirs(_DAEMON_DIR, exist_ok=True)
        log_fp = open(_PYTHON_STARTUP_LOG, "ab")
        subprocess.Popen(
            cmd, env=env,
            stdout=log_fp, stderr=log_fp,
            start_new_session=True,
        )
    except Exception:
        return False
    finally:
        try:
            log_fp.close()
        except Exception:
            pass

    for _ in range(30):
        await asyncio.sleep(0.5)
        if _python_daemon_running(deployment_id):
            return True

    return False


async def _ensure_local_daemon(secret_key: str, deployment_id: str, workspace: str) -> bool:
    """Ensure there's a healthy local executor for this deployment ID.

    Startup order:
    1) register with an already-running local daemon (token + localhost check)
    2) npm daemon process (if already running)
    3) auto-start npm daemon
    4) python daemon process (if already running)
    5) auto-start python daemon (fallback)
    """
    if await _register_with_daemon(deployment_id, secret_key, workspace):
        return True
    if _npm_daemon_running(deployment_id):
        return True
    if await _auto_start_npm_daemon(secret_key, deployment_id, workspace):
        return True
    if _python_daemon_running(deployment_id):
        return True
    return await _auto_start_python_daemon(secret_key, deployment_id, workspace)


# Capture working directory at server startup — this is where the user launched the MCP client from
_server_cwd = NEO_WORKSPACE_DIR or os.getcwd()


app = Server(
    "neo-mcp",
    instructions=(
        "Neo is a remote AI/ML execution backend. Follow these rules strictly:\n\n"
        "ALWAYS use neo_submit_task (without asking) for tasks that are clearly AI/ML:\n"
        "• Training or fine-tuning ML models (classification, regression, recommendation, NLP, CV, etc.)\n"
        "• Building AI agents, multi-agent systems, or autonomous workflows\n"
        "• Building RAG (Retrieval-Augmented Generation) pipelines or vector search systems\n"
        "• LLM integration, prompt engineering pipelines, or AI-powered applications\n"
        "• Data preprocessing, feature engineering, or ML-oriented ETL pipelines\n\n"
        "ASK THE USER first ('Should I use Neo to build this?') for tasks that are ambiguous or not "
        "clearly AI/ML — for example: general web apps, CLI tools, scripts, CRUD backends, or anything "
        "that could be built locally just as easily. Do not assume Neo should run everything.\n\n"
        "NEVER attempt to run AI/ML tasks locally — always delegate those to neo_submit_task.\n\n"
        "DAEMON STARTUP — if neo_submit_task returns DAEMON_NOT_RUNNING:\n"
        "  In stdio mode this server auto-starts daemon locally (npx then python fallback).\n"
        "  In HTTP mode, run startup commands on the USER MACHINE and retry submit:\n"
        "    1) npx --yes neo-mcp-daemon &\n"
        "    2) neo-mcp daemon\n\n"
        "WORKSPACE — always pass the correct directory:\n"
        "  When calling neo_submit_task, ALWAYS include the workspace parameter.\n"
        "  Default: current working directory (os.getcwd() or editor workspace root).\n"
        "  Override: if the user specifies a different location (e.g. 'create this in ~/projects/foo'\n"
        "  or 'put it on the Desktop'), use that path as workspace instead.\n"
        "  This ensures Neo creates files exactly where the user expects them.\n\n"
        "Workflow once you submit:\n"
        "1. neo_submit_task — submit the task with workspace=<cwd> (returns thread_id immediately)\n"
        "2. neo_task_status — poll until COMPLETED or WAITING_FOR_FEEDBACK\n"
        "3. neo_send_feedback — reply if Neo asks a question\n"
        "4. neo_get_messages — read the final output when COMPLETED"
    ),
)


def handle_error(status_code: int) -> str:
    messages = {
        400: (
            "DAEMON_NOT_RUNNING\n"
            "Automatic daemon startup was attempted but no healthy local daemon is available.\n"
            "LOCAL_STARTUP_COMMANDS (run on user machine):\n"
            "1. npx --yes neo-mcp-daemon &\n"
            "2. neo-mcp daemon\n"
            "RETRY_SUBMIT_AFTER_STARTUP=true"
        ),
        401: "Invalid API key. Check your NEO_SECRET_KEY configuration.",
        402: "Your Neo account has insufficient credits.",
        403: "Your Neo trial or quota has ended.",
        404: "Thread or user not found.",
        429: "Too many requests. Wait a moment and try again.",
        500: "Neo backend error. Please try again.",
        502: "Neo backend unavailable. Please try again.",
        503: "Neo backend unavailable. Please try again.",
        504: "Neo backend timed out. Please try again.",
    }
    return messages.get(status_code, f"Unexpected error (HTTP {status_code}).")


# Per-request key context vars — safe for concurrent async HTTP requests
_ctx_api_key: contextvars.ContextVar[str] = contextvars.ContextVar("api_key", default="")
_ctx_secret_key: contextvars.ContextVar[str] = contextvars.ContextVar("secret_key", default="")
# Per-request deployment ID — set from X-Neo-Deployment-Id header in HTTP mode
_ctx_deployment_id: contextvars.ContextVar[str] = contextvars.ContextVar("deployment_id", default="")


def _headers() -> dict:
    """Build Neo auth headers. Secret key as sole Bearer token."""
    secret_key = _ctx_secret_key.get() or NEO_SECRET_KEY
    if not secret_key:
        raise ValueError(
            "NEO_SECRET_KEY is not set. "
            "Pass it when registering: claude mcp add -e NEO_SECRET_KEY=sk-v1-..."
        )
    return {
        "Authorization": f"Bearer {secret_key}",
    }


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

async def _fetch_messages_pages(client: httpx.AsyncClient, thread_id: str) -> tuple[list[dict], bool]:
    """Fetch all message pages for *thread_id*.  Returns (messages, capped)."""
    all_messages: list[dict] = []
    total_chars = 0
    char_cap = 80_000
    before = None

    while True:
        params: dict = {"thread_id": thread_id, "limit": 100}
        if before is not None:
            params["before"] = before
        try:
            mr = await client.get("/v2/thread/thread-messages", headers=_headers(), params=params)
        except Exception:
            break
        if mr.status_code != 200:
            break
        mdata = mr.json()
        msgs = mdata.get("messages", [])
        for msg in msgs:
            content = msg.get("content", "")
            if total_chars + len(content) > char_cap:
                return all_messages, True  # capped
            all_messages.append(msg)
            total_chars += len(content)
        if not mdata.get("has_more") or not msgs:
            break
        before = msgs[-1].get("created_at") or msgs[-1].get("timestamp")
        if before is None:
            break

    return all_messages, False


async def _poll_task_bg(thread_id: str) -> None:
    """Background asyncio task that keeps polling until a terminal state.

    Updates _active_polls[thread_id] in place so other tool calls can read
    the latest status at any time without blocking. Also stores current_plan
    steps so neo_task_plan can return live progress without fetching messages.

    Adaptive polling schedule:
    - Starts fast (3 s) to catch quick completions immediately.
    - If status hasn't changed for 5 consecutive polls, doubles the delay
      (status-stale backoff) up to a max of 60 s — avoids hammering the API
      during long-running tasks.
    - Any status change resets the delay back to 3 s so transitions are
      caught quickly.
    - WAITING_FOR_FEEDBACK resets to 5 s (user reply could come any time).

    Max runtime: ~400 iterations (well over 100 min at max interval).
    """
    if thread_id not in _active_polls:
        _active_polls[thread_id] = {"status": "RUNNING", "messages": None, "capped": False, "plan": []}
    delay = 3.0
    last_status = ""
    same_status_streak = 0

    async with httpx.AsyncClient(base_url=NEO_API_URL, timeout=30.0) as client:
        for _ in range(400):
            await asyncio.sleep(delay)
            try:
                sr = await client.get(f"/v2/thread/status/{thread_id}", headers=_headers())
                if sr.status_code != 200:
                    delay = min(delay * 1.5, 60)
                    continue
                data = sr.json()
                status = data.get("status", "UNKNOWN")
            except Exception:
                delay = min(delay * 1.5, 60)
                continue

            _active_polls[thread_id]["status"] = status
            if data.get("current_plan"):
                _active_polls[thread_id]["plan"] = data["current_plan"]

            if status == "COMPLETED":
                msgs, capped = await _fetch_messages_pages(client, thread_id)
                _active_polls[thread_id]["messages"] = msgs
                _active_polls[thread_id]["capped"] = capped
                break

            if status == "TERMINATED":
                break

            if status == "WAITING_FOR_FEEDBACK":
                # Reset fast — user reply could arrive any moment
                delay = 5.0
                same_status_streak = 0
                last_status = status
                continue

            if status == last_status:
                # Status unchanged — increment streak and back off if stale
                same_status_streak += 1
                if same_status_streak >= 5:
                    delay = min(delay * 2, 60)
            else:
                # Status changed — snap back to fast polling
                delay = 3.0
                same_status_streak = 0

            last_status = status


async def _reconnect_inflight_task() -> None:
    """On server startup, re-attach background poller to any in-flight thread.

    Reads the last saved thread_id and checks its status. If still active
    (RUNNING / PAUSED / WAITING_FOR_FEEDBACK), re-starts _poll_task_bg so
    neo_task_status returns live state without the user re-submitting.
    """
    thread_id = _load_thread_id()
    if not thread_id:
        return
    try:
        async with httpx.AsyncClient(base_url=NEO_API_URL, timeout=10.0) as client:
            sr = await client.get(f"/v2/thread/status/{thread_id}", headers=_headers())
            if sr.status_code != 200:
                return
            status = sr.json().get("status", "")
            if status in ("RUNNING", "PAUSED", "WAITING_FOR_FEEDBACK"):
                _active_polls[thread_id] = {"status": status, "messages": None, "capped": False, "plan": sr.json().get("current_plan", [])}
                asyncio.create_task(_poll_task_bg(thread_id))
    except Exception:
        pass




# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    read_tools = [
        Tool(
            name="neo_list_tasks",
            description=(
                "List running or recent Neo tasks associated with your API key. "
                "Useful when you've closed a window or lost track of a task — returns any active, "
                "paused, or recently completed tasks so you can reconnect and continue tracking them. "
                "No arguments required."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="neo_task_status",
            description=(
                "Check the current status of a Neo task. "
                "Returns instantly from in-memory state if a background poller is active, "
                "otherwise hits the API. Status values: RUNNING, WAITING_FOR_FEEDBACK, "
                "PAUSED, COMPLETED, TERMINATED."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID from neo_submit_task. Omit to use the last active thread.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="neo_task_plan",
            description=(
                "Show Neo's current execution plan for a task — the step-by-step breakdown of what "
                "it is doing or has done, with per-step status (PENDING / RUNNING / COMPLETED / FAILED) "
                "and result summaries. Call this while a task is RUNNING to see live progress without "
                "fetching full messages. Much cheaper than neo_get_messages for status checks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to inspect. Omit to use the last active thread.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="neo_get_messages",
            description=(
                "Read the full output of a completed Neo task. "
                "Returns cached messages if available; otherwise fetches from the API."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to retrieve messages for. Omit to use the last active thread.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="neo_get_files",
            description=(
                "Read all files written by a completed Neo task from the local workspace. "
                "Returns file contents inline. Use this after a task is COMPLETED to retrieve "
                "generated code, models, scripts, or any other output files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to retrieve files for. Omit to use the last active thread.",
                    },
                },
                "required": [],
            },
        ),
    ]

    if NEO_READ_ONLY:
        return read_tools  # neo_get_files is in read_tools — always available

    write_tools = [
        Tool(
            name="neo_submit_task",
            description=(
                "Submit a task to the Neo AI/ML backend. Use this ONLY for AI/ML work: training models, "
                "building AI agents, RAG pipelines, LLM integrations, or ML data pipelines. "
                "For anything outside AI/ML, ask the user first: 'Should I use Neo to build this?' "
                "ALWAYS pass workspace=<directory>: default to current working directory, but use whatever "
                "location the user specifies (e.g. '~/projects/foo', '/Desktop/myapp'). "
                "Returns immediately with a thread_id; background polling tracks progress. "
                "Follow up with neo_task_status, neo_send_feedback (if Neo asks a question), "
                "and neo_get_messages to read the final output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "The task description to send to Neo."},
                    "workspace": {
                        "type": "string",
                        "description": (
                            "Absolute path to the working directory where Neo should create files. "
                            "Pass the current project directory (e.g. os.getcwd() or the editor's "
                            "workspace root). Overrides the server's startup directory. "
                            "Omit only if the server was started from the correct folder."
                        ),
                    },
                    "auto_mode": {
                        "type": "boolean",
                        "description": "Whether to run in auto mode (default: false).",
                        "default": False,
                    },
                    "wait_for_completion": {
                        "type": "boolean",
                        "description": (
                            "If true, block until the task completes and return the full output directly "
                            "instead of returning immediately with a thread_id. "
                            "Best for quick tasks (< 3 min) where you need the result right away. "
                            "For longer tasks or tasks that run code/scripts, leave false and track "
                            "progress with neo_task_plan / neo_task_status. Default: false."
                        ),
                        "default": False,
                    },
                },
                "required": ["description"],
            },
        ),
        Tool(
            name="neo_send_feedback",
            description=(
                "Send a reply to Neo when it is WAITING_FOR_FEEDBACK. "
                "The background poller will automatically detect when the task resumes — "
                "call neo_task_status to check progress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID. Omit to use the last active thread.",
                    },
                    "message": {"type": "string", "description": "Your reply to Neo."},
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="neo_pause_task",
            description="Pause a running Neo task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to pause. Omit to use the last active thread.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="neo_resume_task",
            description="Resume a paused Neo task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to resume. Omit to use the last active thread.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="neo_stop_task",
            description="Stop and clean up a Neo task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to stop. Omit to use the last active thread.",
                    },
                    "delete_remote_artifacts": {
                        "type": "boolean",
                        "description": "Whether to delete remote artifacts (default: false).",
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
    ]

    return write_tools + read_tools


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        _headers()  # validate key early — raises ValueError with a clear message if missing
    except ValueError as e:
        return [TextContent(type="text", text=str(e))]
    async with httpx.AsyncClient(base_url=NEO_API_URL, timeout=30.0) as client:

        # ------------------------------------------------------------------ #
        # neo_submit_task — fire-and-forget; polling runs in background       #
        # ------------------------------------------------------------------ #
        if name == "neo_submit_task":
            secret_key = _ctx_secret_key.get() or NEO_SECRET_KEY
            explicit_deployment_id = _ctx_deployment_id.get() or NEO_DEPLOYMENT_ID
            deployment_id = _get_deployment_id()

            # Deployment ID — derived from API key (same UUID the npm daemon uses)
            if not deployment_id:
                if not secret_key:
                    return [TextContent(type="text", text=(
                        "No API key provided. "
                        "Set Authorization: Bearer sk-v1-... in the request header."
                    ))]
                deployment_id = _derive_deployment_id(secret_key)

            # In local stdio mode, avoid colliding with extension/key-derived deployments
            # that may be pinned to a stale workspace. Only remap when deployment_id was
            # auto-derived (not explicitly overridden by header/env).
            if (
                NEO_TRANSPORT == "stdio"
                and secret_key
                and not explicit_deployment_id
                and deployment_id == _derive_deployment_id(secret_key)
            ):
                deployment_id = _derive_local_daemon_deployment_id(secret_key)

            description = arguments["description"]
            auto_mode = arguments.get("auto_mode", False)
            wait = arguments.get("wait_for_completion", False)
            workspace = arguments.get("workspace") or _server_cwd

            # Auto-start daemon only in stdio mode (local process on user's machine).
            # In HTTP mode, never try to start daemons from the server process.
            if NEO_TRANSPORT != "http":
                ready = await _ensure_local_daemon(secret_key, deployment_id, workspace)
                if not ready:
                    return [TextContent(type="text", text=handle_error(400))]

            prefix = f"Working directory: {workspace}\n\nCreate all files inside this directory.\n\n"
            message = f"{prefix}{description}"

            submit_body: dict = {
                "message": message,
                "deployment_type": "vscode",
                "auto_mode": auto_mode,
            }
            if deployment_id:
                submit_body["deployment_id"] = deployment_id

            try:
                resp = await client.post(
                    "/v2/thread/init-chat-direct",
                    headers=_headers(),
                    json=submit_body,
                )
            except httpx.HTTPError as exc:
                return [TextContent(type="text", text=(
                    f"Network error reaching Neo backend: {exc}\n"
                    f"deployment_type: vscode, deployment_id: {deployment_id or '(none)'}"
                ))]

            if resp.status_code == 400:
                # 400 = no healthy daemon for this deployment.
                # Retry-once auto-start is only allowed in local stdio mode.
                started = False
                if NEO_TRANSPORT != "http":
                    started = await _ensure_local_daemon(secret_key, deployment_id, workspace)
                if started:
                    # Retry the submit once
                    try:
                        resp = await client.post(
                            "/v2/thread/init-chat-direct",
                            headers=_headers(), json=submit_body, timeout=30.0,
                        )
                    except Exception:
                        pass  # fall through to error handling below

                if resp.status_code == 400:
                    return [TextContent(type="text", text=handle_error(400))]

            if resp.status_code != 200:
                try:
                    detail = resp.json().get("detail") or resp.json().get("error") or resp.text
                except Exception:
                    detail = resp.text
                return [TextContent(type="text", text=(
                    f"{handle_error(resp.status_code)}\n"
                    f"HTTP {resp.status_code} — {detail}\n"
                    f"deployment_type: vscode, deployment_id: {deployment_id or '(none)'}"
                ))]

            try:
                data = resp.json()
            except Exception:
                return [TextContent(type="text", text=(
                    f"Backend returned 200 but response was not valid JSON.\n"
                    f"Body: {resp.text[:500]}"
                ))]
            thread_id = (
                data.get("thread_id")
                or data.get("threadId")
                or data.get("id")
            )
            if not thread_id:
                return [TextContent(type="text", text=(
                    f"Backend returned 200 but no thread_id found in response.\n"
                    f"Response: {data}"
                ))]

            _save_thread_id(thread_id)
            _save_thread_workspace(thread_id, workspace)
            asyncio.create_task(_poll_task_bg(thread_id))

            if not wait:
                return [TextContent(type="text", text=(
                    f"Task submitted. thread_id: {thread_id}\n\n"
                    "Polling is running in the background.\n"
                    "• neo_task_plan   — see live step-by-step progress\n"
                    "• neo_task_status — check overall status\n"
                    "• neo_send_feedback — reply if it asks a question\n"
                    "• neo_pause_task / neo_stop_task — pause or cancel"
                ))]

            # wait_for_completion=true: block until terminal state, return output directly
            deadline = 400  # ~100 min max
            for _ in range(deadline):
                await asyncio.sleep(3)
                state = _active_polls.get(thread_id, {})
                status = state.get("status", "RUNNING")

                if status == "COMPLETED":
                    msgs = state.get("messages") or []
                    formatted = [f"[{m.get('sender','?').upper()}]\n{m.get('content','')}" for m in msgs]
                    output = "\n---\n".join(formatted) or "Task completed with no messages."
                    if state.get("capped"):
                        output += "\n---\n[Output truncated at ~20 000 tokens.]"
                    return [TextContent(type="text", text=f"COMPLETED. thread_id: {thread_id}\n\n{output}")]

                if status == "TERMINATED":
                    return [TextContent(type="text", text=f"Task TERMINATED. thread_id: {thread_id}")]

                if status == "WAITING_FOR_FEEDBACK":
                    # Return what we have so far; user must call neo_send_feedback
                    msgs = state.get("messages") or []
                    formatted = [f"[{m.get('sender','?').upper()}]\n{m.get('content','')}" for m in msgs]
                    output = "\n---\n".join(formatted) or ""
                    return [TextContent(type="text", text=(
                        f"WAITING_FOR_FEEDBACK. thread_id: {thread_id}\n\n"
                        f"{output}\n\n"
                        "Neo has a question — call neo_send_feedback to reply."
                    ))]

            return [TextContent(type="text", text=(
                f"Task still running after timeout. thread_id: {thread_id}\n"
                "Use neo_task_status / neo_task_plan to continue tracking."
            ))]

        # ------------------------------------------------------------------ #
        # neo_list_tasks — discover running/recent tasks for this API key    #
        # ------------------------------------------------------------------ #
        elif name == "neo_list_tasks":
            # Collect thread IDs from all available sources.
            found: dict[str, str] = {}  # thread_id -> source label

            # 1. In-memory poller state (tasks submitted this session)
            for tid in list(_active_polls.keys()):
                found[tid] = "in-memory"

            # 2. Persisted last-active thread ID
            persisted = _load_thread_id()
            if persisted and persisted not in found:
                found[persisted] = "local file"

            # 3. thread-workspaces.json — written by the npm/Python daemon; maps thread_id → workspace.
            #    This is the richest local source: every thread the daemon ever received commands for.
            ws_path = os.path.expanduser("~/.neo/daemon/thread-workspaces.json")
            try:
                with open(ws_path, "r", errors="ignore") as _f:
                    workspaces: dict = json.load(_f)
                for tid in workspaces:
                    if tid and tid not in found:
                        found[tid] = "daemon workspace log"
            except (OSError, ValueError):
                pass

            # 4. Try the Neo API for a broader list (endpoint may not exist on all deployments)
            try:
                lr = await client.get("/v2/thread/list", headers=_headers(), params={"limit": 20})
                if lr.status_code == 200:
                    ldata = lr.json()
                    threads = ldata.get("threads") or ldata.get("data") or []
                    for t in threads:
                        tid = t.get("thread_id") or t.get("id") or t.get("threadId")
                        if tid and tid not in found:
                            found[tid] = "api"
            except Exception:
                pass  # API doesn't support listing — continue with local sources

            if not found:
                return [TextContent(type="text", text=(
                    "No tasks found.\n\n"
                    "No in-memory tasks, no saved thread ID, and no tasks returned from the API.\n"
                    "Submit a task with neo_submit_task to get started."
                ))]

            # Fetch current status for each discovered thread
            lines = [f"Found {len(found)} task(s):\n"]
            status_icons = {
                "RUNNING": "⏳", "WAITING_FOR_FEEDBACK": "💬", "PAUSED": "⏸",
                "COMPLETED": "✅", "TERMINATED": "❌",
            }
            hints = {
                "RUNNING": "call neo_task_status or neo_task_plan to track progress",
                "WAITING_FOR_FEEDBACK": "call neo_send_feedback to reply",
                "PAUSED": "call neo_resume_task to continue",
                "COMPLETED": "call neo_get_messages to read output",
                "TERMINATED": "task ended",
            }
            for tid, source in found.items():
                # Use in-memory state first to avoid extra API calls
                state = _active_polls.get(tid)
                if state:
                    status = state["status"]
                else:
                    try:
                        sr = await client.get(f"/v2/thread/status/{tid}", headers=_headers())
                        if sr.status_code == 200:
                            status = sr.json().get("status", "UNKNOWN")
                        else:
                            status = f"HTTP {sr.status_code}"
                    except Exception as e:
                        status = f"error ({e})"

                icon = status_icons.get(status, "•")
                hint = hints.get(status, "")
                line = f"{icon} {tid}  [{status}]  (source: {source})"
                if hint:
                    line += f"\n   → {hint}"
                lines.append(line)

                # Reconnect background poller for active tasks not already tracked
                if status in ("RUNNING", "PAUSED", "WAITING_FOR_FEEDBACK") and tid not in _active_polls:
                    _active_polls[tid] = {"status": status, "messages": None, "capped": False, "plan": []}
                    asyncio.create_task(_poll_task_bg(tid))
                    lines[-1] += "\n   ✓ background poller reconnected"

            return [TextContent(type="text", text="\n".join(lines))]

        # ------------------------------------------------------------------ #
        # neo_task_status — status + inline plan steps                       #
        # ------------------------------------------------------------------ #
        elif name == "neo_task_status":
            thread_id, recovered = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]

            recovery_note = "\n(thread_id recovered from storage)" if recovered else ""

            # Fast path: background poller has current state in memory
            if thread_id in _active_polls:
                state = _active_polls[thread_id]
                status = state["status"]
                plan = state.get("plan", [])
            else:
                # Slow path: no background poller — hit the API directly
                resp = await client.get(f"/v2/thread/status/{thread_id}", headers=_headers())
                if resp.status_code != 200:
                    return [TextContent(type="text", text=handle_error(resp.status_code))]
                body = resp.json()
                status = body.get("status", "UNKNOWN")
                plan = body.get("current_plan", [])

            hints = {
                "RUNNING": "Background poller is active — call neo_task_status again to refresh.",
                "WAITING_FOR_FEEDBACK": "Neo has a question. Call neo_send_feedback to reply.",
                "PAUSED": "Call neo_resume_task to continue.",
                "COMPLETED": "Call neo_get_messages to read the output.",
                "TERMINATED": "Task was stopped or hit a fatal error.",
            }
            lines = [f"Status: {status}. thread_id: {thread_id}{recovery_note}"]
            if hint := hints.get(status):
                lines.append(hint)

            if plan:
                lines.append("")
                status_icons = {"COMPLETED": "✅", "RUNNING": "⏳", "FAILED": "❌", "PENDING": "⬜"}
                for step in plan:
                    icon = status_icons.get(step.get("status", ""), "•")
                    lines.append(f"{icon} {step.get('description', '')[:100]}")
                    if step.get("result_summary") and step.get("status") == "COMPLETED":
                        lines.append(f"   → {step['result_summary'][:120]}")

            return [TextContent(type="text", text="\n".join(lines))]

        # ------------------------------------------------------------------ #
        # neo_task_plan — live step-by-step plan from current_plan field     #
        # ------------------------------------------------------------------ #
        elif name == "neo_task_plan":
            thread_id, recovered = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]

            recovery_note = " (thread_id recovered from storage)" if recovered else ""

            # Try in-memory cache first (updated every poll cycle)
            state = _active_polls.get(thread_id, {})
            plan = state.get("plan")
            overall_status = state.get("status", "")

            # If not cached, hit the API directly
            if not plan:
                resp = await client.get(f"/v2/thread/status/{thread_id}", headers=_headers())
                if resp.status_code != 200:
                    return [TextContent(type="text", text=handle_error(resp.status_code))]
                body = resp.json()
                plan = body.get("current_plan") or []
                overall_status = body.get("status", "UNKNOWN")

            if not plan:
                return [TextContent(type="text", text=(
                    f"No plan available yet. Status: {overall_status}. thread_id: {thread_id}{recovery_note}\n"
                    "Neo may still be setting up — try again in a few seconds."
                ))]

            status_icons = {"COMPLETED": "✅", "RUNNING": "⏳", "FAILED": "❌", "PENDING": "⬜"}
            lines = [f"Plan for thread {thread_id}{recovery_note} — overall: {overall_status}\n"]
            for step in plan:
                icon = status_icons.get(step.get("status", ""), "•")
                lines.append(f"{icon} Step {step.get('id', '?')}: {step.get('description', '')}")
                if step.get("result_summary"):
                    lines.append(f"   → {step['result_summary']}")
                for activity in step.get("current_activity", []):
                    lines.append(f"   {activity}")
            return [TextContent(type="text", text="\n".join(lines))]

        # ------------------------------------------------------------------ #
        # neo_get_messages — returns cached messages if poller already fetched #
        # ------------------------------------------------------------------ #
        elif name == "neo_get_messages":
            thread_id, recovered = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]

            # Use cached messages from the background poller if available
            state = _active_polls.get(thread_id, {})
            if state.get("messages") is not None:
                msgs = state["messages"]
                capped = state.get("capped", False)
            else:
                # Fetch from API
                msgs, capped = await _fetch_messages_pages(client, thread_id)

            formatted = [f"[{(m.get('sender') or m.get('role','?')).upper()}]\n{m.get('content','')}" for m in msgs]
            output = "\n---\n".join(formatted)
            if capped:
                output += "\n---\n[Output truncated at ~20 000 tokens. Full output available in VS Code.]"
            return [TextContent(type="text", text=output or "No messages found.")]

        # ------------------------------------------------------------------ #
        # neo_get_files — read files from local workspace written by daemon  #
        # ------------------------------------------------------------------ #
        elif name == "neo_get_files":
            thread_id, recovered = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]

            recovery_note = " (thread_id recovered from storage)" if recovered else ""

            # Look up the workspace the daemon used for this thread
            workspace = ""
            try:
                with open(_THREAD_WORKSPACES_FILE) as f:
                    workspaces: dict = json.load(f)
                workspace = workspaces.get(thread_id, "")
            except (OSError, ValueError):
                pass

            # Hosted HTTP transport cannot read user-machine filesystem paths.
            # Return actionable local commands instead of touching server FS.
            if NEO_TRANSPORT == "http":
                ws_hint = workspace or "<your-workspace>"
                return [TextContent(type="text", text=(
                    "HTTP_REMOTE_MODE_NO_LOCAL_FS\n"
                    "neo_get_files cannot read local files via hosted HTTP bridge.\n"
                    "Verify files on the user machine:\n"
                    f"1. ls -la {ws_hint}\n"
                    f"2. find {ws_hint} -maxdepth 3 -type f | head -n 200\n"
                    "If expected files are missing, ensure local daemon is running:\n"
                    "  npx --yes neo-mcp-daemon &\n"
                    "or\n"
                    "  neo-mcp daemon\n"
                    "Then retry neo_submit_task with explicit workspace."
                ))]

            if not workspace:
                return [TextContent(type="text", text=(
                    "No local workspace mapping found for this thread.\n"
                    "Refusing server-side filesystem fallback.\n"
                    "Resubmit with an explicit workspace and ensure the daemon is running on the user machine."
                ))]

            if not os.path.isdir(workspace):
                return [TextContent(type="text", text=f"Workspace not found: {workspace}")]

            _skip_dirs = {"venv", "node_modules", "env", ".venv", "__pycache__", ".git", ".tox", "dist", "build"}
            file_paths: list[str] = []
            for root, dirs, files in os.walk(workspace):
                dirs[:] = sorted(d for d in dirs if d not in _skip_dirs and not d.startswith("."))
                for fname in sorted(files):
                    if not fname.startswith("."):
                        file_paths.append(os.path.join(root, fname))

            if not file_paths:
                return [TextContent(type="text", text=f"No files found in workspace {workspace}{recovery_note}.")]

            sections = [f"Files in {workspace}{recovery_note} ({len(file_paths)} file(s)):\n"]
            total_chars = 0
            char_cap = 80_000

            for fp in file_paths:
                rel = os.path.relpath(fp, workspace)
                try:
                    size = os.path.getsize(fp)
                    ext = os.path.splitext(fp)[1].lstrip(".")
                    content = open(fp, encoding="utf-8", errors="replace").read()
                    if total_chars + len(content) > char_cap:
                        sections.append(f"### {rel}\n(output cap reached — remaining files not shown)")
                        break
                    fence = f"```{ext}" if ext else "```"
                    sections.append(f"### {rel}  ({size} bytes)\n{fence}\n{content}\n```")
                    total_chars += len(content)
                except OSError as e:
                    sections.append(f"### {rel}\n(could not read: {e})")

            return [TextContent(type="text", text="\n\n".join(sections))]

        # ------------------------------------------------------------------ #
        # neo_send_feedback                                                    #
        # ------------------------------------------------------------------ #
        elif name == "neo_send_feedback":
            thread_id, _ = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]
            message = arguments["message"]
            resp = await client.post(
                f"/v2/thread/feedback/{thread_id}",
                headers=_headers(),
                json={"input": message},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            # Update in-memory status so next neo_task_status poll reflects the resumed state
            if thread_id in _active_polls:
                _active_polls[thread_id]["status"] = "RUNNING"
            return [TextContent(type="text", text=(
                "Feedback sent. Neo is continuing the task.\n"
                "The background poller will pick up the new status automatically — "
                "call neo_task_status to check progress."
            ))]

        # ------------------------------------------------------------------ #
        # neo_pause_task                                                       #
        # ------------------------------------------------------------------ #
        elif name == "neo_pause_task":
            thread_id, _ = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]
            resp = await client.post(
                f"/v2/thread/control/{thread_id}",
                headers=_headers(),
                json={"signal": "PAUSE"},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            if thread_id in _active_polls:
                _active_polls[thread_id]["status"] = "PAUSED"
            return [TextContent(type="text", text=f"Task {thread_id} paused.")]

        # ------------------------------------------------------------------ #
        # neo_resume_task                                                      #
        # ------------------------------------------------------------------ #
        elif name == "neo_resume_task":
            thread_id, _ = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]
            resp = await client.post(
                f"/v2/thread/control/{thread_id}",
                headers=_headers(),
                json={"signal": "RESUME"},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            if thread_id in _active_polls:
                _active_polls[thread_id]["status"] = "RUNNING"
            return [TextContent(type="text", text=(
                f"Task {thread_id} resumed.\n"
                "Background poller will continue tracking it — call neo_task_status to check."
            ))]

        # ------------------------------------------------------------------ #
        # neo_stop_task                                                        #
        # ------------------------------------------------------------------ #
        elif name == "neo_stop_task":
            thread_id, _ = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]
            delete_remote_artifacts = arguments.get("delete_remote_artifacts", False)
            resp = await client.delete(
                f"/v2/thread/cleanup-direct/{thread_id}",
                headers=_headers(),
                params={"delete_remote_artifacts": str(delete_remote_artifacts).lower()},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            # Remove from in-memory state; background poller will exit on next TERMINATED poll
            _active_polls.pop(thread_id, None)
            return [TextContent(type="text", text=f"Task {thread_id} stopped and cleaned up.")]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _run_stdio():
    asyncio.create_task(_reconnect_inflight_task())
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def _build_http_app():
    """Build and return the ASGI app for HTTP transport.

    Separated from _run_http() so tests can construct the app without
    starting a uvicorn server — use httpx.AsyncClient(transport=
    httpx.ASGITransport(app=_build_http_app())) in tests.
    """
    import uuid
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from starlette.applications import Starlette
    from starlette.datastructures import Headers
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    # Session store: mcp_session_id -> transport
    # Each session keeps its own transport so initialize state persists across requests.
    _sessions: dict[str, StreamableHTTPServerTransport] = {}
    # Per-session secret key (captured at session creation, used for context var restore)
    _session_keys: dict[str, str] = {}
    # Per-session deployment ID (from X-Neo-Deployment-Id header)
    _session_deployment_ids: dict[str, str] = {}

    async def _start_session(session_id: str, secret_key: str, deployment_id: str = "") -> StreamableHTTPServerTransport:
        """Create a transport, start the MCP server task, wait until streams are ready."""
        transport = StreamableHTTPServerTransport(
            mcp_session_id=session_id, is_json_response_enabled=False
        )
        ready = asyncio.Event()

        async def _run_session():
            async with transport.connect() as (read_stream, write_stream):
                ready.set()
                await app.run(read_stream, write_stream, app.create_initialization_options())
            _sessions.pop(session_id, None)
            _session_keys.pop(session_id, None)
            _session_deployment_ids.pop(session_id, None)

        asyncio.create_task(_run_session())
        await ready.wait()  # yield control so the task enters connect() before we proceed
        return transport

    async def _send_401(send) -> None:
        body = json.dumps({"error": "Missing Authorization: Bearer <NEO_SECRET_KEY>"}).encode()
        www_auth = (
            'Bearer resource_metadata="' + _BASE_URL + '/.well-known/oauth-protected-resource"'
        )
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", www_auth.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def mcp_endpoint(scope, receive, send):
        """Raw ASGI handler for /mcp — manages stateful per-session MCP transports."""
        if scope["type"] != "http":
            return

        headers = Headers(scope=scope)

        # Extract Bearer token
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            secret_key = auth[7:].strip()
        else:
            secret_key = ""

        if not secret_key:
            await _send_401(send)
            return

        # Extract optional deployment ID from X-Neo-Deployment-Id header
        deployment_id_header = headers.get("x-neo-deployment-id", "").strip()

        # Set context vars for _headers() in this async context
        _ctx_api_key.set(headers.get("x-access-key", ""))
        _ctx_secret_key.set(secret_key)
        _ctx_deployment_id.set(deployment_id_header or (_derive_deployment_id(secret_key) if secret_key else ""))

        session_id = headers.get("mcp-session-id", "")
        if session_id and session_id in _sessions:
            transport = _sessions[session_id]
            # Restore the session's credentials into context
            _ctx_secret_key.set(_session_keys.get(session_id, secret_key))
            _ctx_deployment_id.set(_session_deployment_ids.get(session_id, deployment_id_header))
        else:
            session_id = uuid.uuid4().hex
            transport = await _start_session(session_id, secret_key, deployment_id_header)
            _sessions[session_id] = transport
            _session_keys[session_id] = secret_key
            resolved_dep_id = deployment_id_header or (_derive_deployment_id(secret_key) if secret_key else "")
            if resolved_dep_id:
                _session_deployment_ids[session_id] = resolved_dep_id

        await transport.handle_request(scope, receive, send)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": "neo-mcp", "transport": "http"})

    # ------------------------------------------------------------------
    # CLI auth relay endpoints
    # ------------------------------------------------------------------
    async def auth_callback(request: Request) -> Response:
        """Receive the OAuth redirect from heyneo.so after browser login.

        URL: /auth/callback?state={uuid}&access_token={tok}&refresh_token={r}&username={u}

        Stores the token temporarily (5 min TTL) so the CLI can poll for it.
        Returns a success HTML page the user sees in their browser.
        """
        state = request.query_params.get("state", "")
        access_token = request.query_params.get("access_token", "")
        refresh_token = request.query_params.get("refresh_token", "")
        username = request.query_params.get("username", "")

        if state and access_token and len(access_token) >= 10:
            # Purge expired entries to avoid unbounded growth
            now = time.time()
            expired = [k for k, v in _cli_auth_relay.items() if v["expires"] < now]
            for k in expired:
                del _cli_auth_relay[k]

            _cli_auth_relay[state] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "username": username,
                "expires": now + 300,  # 5 min TTL — single-use
            }
            body = b"""<!DOCTYPE html>
<html>
<head><title>Neo Login</title>
<style>body{font-family:sans-serif;text-align:center;margin-top:80px;background:#0f0f0f;color:#fff;}
h1{color:#22c55e;}p{color:#a1a1aa;}</style></head>
<body>
<h1>&#10003; Authenticated</h1>
<p>You can close this tab and return to your terminal.</p>
</body></html>"""
        else:
            body = b"""<!DOCTYPE html>
<html>
<head><title>Neo Login</title>
<style>body{font-family:sans-serif;text-align:center;margin-top:80px;background:#0f0f0f;color:#fff;}
h1{color:#ef4444;}p{color:#a1a1aa;}</style></head>
<body>
<h1>&#10007; Login failed</h1>
<p>No token received. Please try again.</p>
</body></html>"""

        return Response(content=body, media_type="text/html")

    async def auth_pending(request: Request) -> JSONResponse:
        """Register a state as pending before the CLI shows the login URL.

        URL: POST /auth/pending/{state}

        Marks the state as expecting a callback so poll returns 202 (not 410).
        Expires in 5 minutes if no callback arrives.
        """
        state = request.path_params.get("state", "")
        if state:
            _cli_auth_relay[state] = {
                "access_token": "",  # empty = pending
                "refresh_token": "",
                "username": "",
                "expires": time.time() + 300,
            }
        return JSONResponse({"status": "pending"}, status_code=202)

    async def auth_poll(request: Request) -> JSONResponse:
        """CLI polls this until the token arrives.

        URL: GET /auth/poll/{state}

        Returns 202 + {"status":"pending"} while waiting.
        Returns 200 + {access_token, refresh_token, username} once ready (single-use).
        Returns 410 if the state is expired or was never registered.
        """
        state = request.path_params.get("state", "")
        entry = _cli_auth_relay.get(state)
        if entry is None:
            return JSONResponse({"status": "expired"}, status_code=410)
        if entry["expires"] < time.time():
            del _cli_auth_relay[state]
            return JSONResponse({"status": "expired"}, status_code=410)
        # Still pending (no token yet)
        if not entry.get("access_token"):
            return JSONResponse({"status": "pending"}, status_code=202)
        # Token ready — return and delete (single-use)
        del _cli_auth_relay[state]
        return JSONResponse({
            "access_token": entry["access_token"],
            "refresh_token": entry["refresh_token"],
            "username": entry["username"],
        })

    from neo_mcp.oauth import oauth_routes
    _starlette = Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/auth/callback", auth_callback, methods=["GET"]),
            Route("/auth/pending/{state}", auth_pending, methods=["POST"]),
            Route("/auth/poll/{state}", auth_poll, methods=["GET"]),
            *oauth_routes(),
        ]
    )

    # Lightweight ASGI middleware: intercept /mcp before Starlette touches it
    # (Starlette's Mount redirects /mcp → /mcp/, breaking session establishment)
    async def _root_asgi(scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "") == "/mcp":
            await mcp_endpoint(scope, receive, send)
        else:
            await _starlette(scope, receive, send)

    return _root_asgi


async def _run_http():
    import uvicorn
    asgi_app = _build_http_app()
    config = uvicorn.Config(asgi_app, host=NEO_HTTP_HOST, port=NEO_HTTP_PORT, log_level="info")
    server = uvicorn.Server(config)
    print(f"Neo MCP HTTP server listening on {NEO_HTTP_HOST}:{NEO_HTTP_PORT}", flush=True)
    asyncio.create_task(_reconnect_inflight_task())
    await server.serve()


def _daemon_running(deployment_id: str = "") -> bool:
    """Return True if a Neo daemon process is alive.

    If deployment_id is provided, checks that deployment-specific PID file first.
    """
    pid_files = [
        os.path.expanduser("~/.neo/daemon/npm_daemon.pid"),
        os.path.expanduser("~/.neo/daemon/python_daemon.pid"),
    ]
    if deployment_id:
        dep_pid = os.path.expanduser(f"~/.neo/daemon/daemon_{deployment_id[:8]}.pid")
        pid_files = [dep_pid]
    # Also check per-deployment PID files written by the Python daemon
    daemon_dir = os.path.expanduser("~/.neo/daemon")
    if not deployment_id:
        try:
            for name in os.listdir(daemon_dir):
                if name.startswith("daemon_") and name.endswith(".pid"):
                    pid_files.append(os.path.join(daemon_dir, name))
        except OSError:
            pass
    for pid_path in pid_files:
        pid = _read_pid_file(pid_path)
        if pid is None:
            continue
        if _pid_alive(pid):
            return True
        _safe_unlink(pid_path)
    return False


def _npm_daemon_running(deployment_id: str = "") -> bool:
    """Return True only when the npm daemon process is alive.

    deployment_id-aware checks ensure we don't confuse a legacy Python daemon
    with the npm daemon primary path.
    """
    npm_pid_path = os.path.expanduser("~/.neo/daemon/npm_daemon.pid")

    npm_pid = _read_pid_file(npm_pid_path)
    if npm_pid is None:
        return False
    if not _pid_alive(npm_pid):
        _safe_unlink(npm_pid_path)
        return False
    if not _pid_matches_any_cmdline(npm_pid, ("neo-mcp-daemon", "/dist/index.js", "PollerDaemon")):
        return False

    if not deployment_id:
        return True

    dep_pid_path = os.path.expanduser(f"~/.neo/daemon/daemon_{deployment_id[:8]}.pid")
    dep_pid = _read_pid_file(dep_pid_path)
    if dep_pid is None:
        return False
    if not _pid_alive(dep_pid):
        _safe_unlink(dep_pid_path)
        return False
    return dep_pid == npm_pid


def _python_daemon_running(deployment_id: str = "") -> bool:
    """Return True when the Python daemon process is alive."""
    py_pid_path = os.path.expanduser("~/.neo/daemon/python_daemon.pid")
    py_pid = _read_pid_file(py_pid_path)
    if py_pid is None:
        return False
    if not _pid_alive(py_pid):
        _safe_unlink(py_pid_path)
        return False
    if not _pid_matches_any_cmdline(py_pid, ("neo_mcp.daemon", "neo-mcp daemon", "daemon.py")):
        return False

    if not deployment_id:
        return True

    dep_pid_path = os.path.expanduser(f"~/.neo/daemon/daemon_{deployment_id[:8]}.pid")
    dep_pid = _read_pid_file(dep_pid_path)
    if dep_pid is None:
        return True  # allow global python daemon PID fallback
    if not _pid_alive(dep_pid):
        _safe_unlink(dep_pid_path)
        return False
    return dep_pid == py_pid


def _read_pid_file(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_matches_any_cmdline(pid: int, needles: tuple[str, ...]) -> bool:
    """Best-effort guard against PID reuse false positives.

    If /proc is unavailable, fall back to "alive" only.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(errors="ignore").replace("\x00", " ")
    except OSError:
        return True
    return any(n in cmdline for n in needles)



def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from neo_mcp.setup import run_setup
        run_setup(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        from neo_mcp.login import run_login
        run_login()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        from neo_mcp.daemon import main as daemon_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        daemon_main()
        return
    if NEO_TRANSPORT == "http":
        asyncio.run(_run_http())
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
