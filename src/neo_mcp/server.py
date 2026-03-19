import asyncio
import contextvars
import json
import os
import re
import uuid

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server  # used as async context manager
from mcp.types import Tool, TextContent
from mcp import types

NEO_API_URL = os.environ.get("NEO_API_URL", "https://master.heyneo.so")
NEO_API_KEY = os.environ.get("NEO_API_KEY", "")      # access key (ak-v1-...)
NEO_SECRET_KEY = os.environ.get("NEO_SECRET_KEY", "") # secret key (sk-v1-...)
NEO_READ_ONLY = os.environ.get("NEO_READ_ONLY", "").lower() == "true"
NEO_DEPLOYMENT_ID = os.environ.get("NEO_DEPLOYMENT_ID", "")  # optional, override auto-discovered sandbox ID
NEO_WORKSPACE_DIR = os.environ.get("NEO_WORKSPACE_DIR", "")  # optional, override CWD (useful in Docker)
NEO_TRANSPORT = os.environ.get("NEO_TRANSPORT", "stdio").lower()  # "stdio" or "http"
NEO_HTTP_PORT = int(os.environ.get("NEO_HTTP_PORT") or os.environ.get("PORT", "8000"))
NEO_HTTP_HOST = os.environ.get("NEO_HTTP_HOST", "0.0.0.0")

_THREAD_ID_FILE = os.path.expanduser("~/.neo/active_thread_id")

# In-memory poll state: { thread_id: { "status": str, "messages": list|None, "capped": bool } }
# Populated and updated by background asyncio tasks; read by neo_task_status / neo_get_messages.
_active_polls: dict[str, dict] = {}


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
# Sandbox / deployment ID discovery
# ---------------------------------------------------------------------------

def _discover_sandbox_id() -> str:
    """Try multiple sources to find the active Neo sandbox/deployment ID.

    Priority:
    1. daemon.log  — most recent sandboxId (works when VS Code / Cursor extension is running)
    2. thread-workspaces.json — match current working directory to a sandbox ID
    Returns empty string if nothing found.
    """
    cwd = os.getcwd()

    # 1. daemon log — works for VS Code and Cursor Neo extensions
    for log_name in ("daemon.log", "daemon.log.1"):
        log_path = os.path.expanduser(f"~/.neo/daemon/{log_name}")
        try:
            with open(log_path, "r", errors="ignore") as f:
                content = f.read()
            matches = re.findall(r'"sandboxId"\s*:\s*"([a-f0-9\-]{36})"', content)
            if matches:
                return matches[-1]
        except OSError:
            pass

    # 2. thread-workspaces.json — maps sandbox IDs to workspace paths
    ws_path = os.path.expanduser("~/.neo/daemon/thread-workspaces.json")
    try:
        with open(ws_path, "r", errors="ignore") as f:
            workspaces: dict = json.load(f)
        # Prefer exact CWD match, then parent match
        for sandbox_id, ws_dir in reversed(list(workspaces.items())):
            if cwd == ws_dir or cwd.startswith(ws_dir.rstrip("/") + "/"):
                return sandbox_id
        # Fallback: return the most recent entry
        if workspaces:
            return list(workspaces.keys())[-1]
    except (OSError, ValueError):
        pass

    return ""


def _get_deployment_id() -> str:
    """Return deployment ID — re-discovers each call so extensions that start
    after the MCP server are picked up automatically."""
    return NEO_DEPLOYMENT_ID or _discover_sandbox_id()


# Capture working directory at server startup — this is where the user launched the MCP client from
_server_cwd = NEO_WORKSPACE_DIR or os.getcwd()


def _check_config():
    # In HTTP mode, keys can be supplied per-request via headers — env vars are optional
    if NEO_TRANSPORT == "stdio":
        if not NEO_API_KEY:
            raise ValueError("NEO_API_KEY environment variable is required but not set.")
        if not NEO_SECRET_KEY:
            raise ValueError("NEO_SECRET_KEY environment variable is required but not set.")


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
        "Workflow once you submit:\n"
        "1. neo_submit_task — submit the task (returns thread_id immediately)\n"
        "2. neo_task_status — poll until COMPLETED or WAITING_FOR_FEEDBACK\n"
        "3. neo_send_feedback — reply if Neo asks a question\n"
        "4. neo_get_messages — read the final output when COMPLETED"
    ),
)


def handle_error(status_code: int) -> str:
    messages = {
        400: "No available deployment. The built-in daemon may still be starting — retry in a few seconds.",
        401: "Invalid API key. Check your NEO_API_KEY configuration.",
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


def _headers() -> dict:
    """Build Neo auth headers. Per-request context vars take priority over env vars."""
    api_key = _ctx_api_key.get() or NEO_API_KEY
    secret_key = _ctx_secret_key.get() or NEO_SECRET_KEY
    return {
        "Authorization": f"Bearer {secret_key}",
        "x-access-key": api_key,
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
    the latest status at any time without blocking.

    Polling schedule: starts at 3 s, ramps up to 15 s max.
    Does NOT stop on WAITING_FOR_FEEDBACK — it keeps polling so the task
    auto-resumes (and status updates) after neo_send_feedback is called.

    Max runtime: ~400 iterations × 15 s ≈ 100 minutes.
    """
    _active_polls[thread_id] = {"status": "RUNNING", "messages": None, "capped": False}
    delay = 3.0

    async with httpx.AsyncClient(base_url=NEO_API_URL, timeout=30.0) as client:
        for _ in range(400):
            await asyncio.sleep(delay)
            try:
                sr = await client.get(f"/v2/thread/status/{thread_id}", headers=_headers())
                if sr.status_code != 200:
                    delay = min(delay * 1.5, 15)
                    continue
                status = sr.json().get("status", "UNKNOWN")
            except Exception:
                delay = min(delay * 1.5, 15)
                continue

            _active_polls[thread_id]["status"] = status

            if status == "COMPLETED":
                msgs, capped = await _fetch_messages_pages(client, thread_id)
                _active_polls[thread_id]["messages"] = msgs
                _active_polls[thread_id]["capped"] = capped
                break

            if status == "TERMINATED":
                break

            if status == "WAITING_FOR_FEEDBACK":
                # Keep polling — will naturally catch the transition back to RUNNING
                # once the user sends feedback via neo_send_feedback.
                delay = 5.0
                continue

            # RUNNING / PAUSED / unknown — ramp delay up to 15 s
            delay = min(delay * 1.3, 15)


# ---------------------------------------------------------------------------
# Built-in daemon — polls backend and executes commands locally
# Eliminates the VS Code extension requirement: the MCP server IS the daemon.
# ---------------------------------------------------------------------------

_STANDALONE_ID_FILE = os.path.expanduser("~/.neo/daemon/standalone_deployment_id")

# Active subprocess jobs: { job_id: {"stdout": str, "stderr": str, "exit_code": int|None, "proc": Process|None} }
_daemon_jobs: dict[str, dict] = {}


def _get_or_create_deployment_id() -> str:
    """Load a stable deployment UUID or generate one on first run."""
    try:
        os.makedirs(os.path.dirname(_STANDALONE_ID_FILE), exist_ok=True)
        if os.path.exists(_STANDALONE_ID_FILE):
            uid = open(_STANDALONE_ID_FILE).read().strip()
            if uid:
                return uid
        uid = str(uuid.uuid4())
        with open(_STANDALONE_ID_FILE, "w") as f:
            f.write(uid)
        return uid
    except OSError:
        return str(uuid.uuid4())


def _write_sandbox_log_entry(deployment_id: str) -> None:
    """Append sandboxId entry so _discover_sandbox_id() auto-detects it."""
    log_path = os.path.expanduser("~/.neo/daemon/daemon.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps({"sandboxId": deployment_id, "source": "python-daemon"}) + "\n")
    except OSError:
        pass


def _safe_resolve(path_str: str, workspace: str) -> str | None:
    """Return realpath if within workspace or /tmp, else None."""
    if os.path.isabs(path_str):
        resolved = os.path.realpath(path_str)
    else:
        resolved = os.path.realpath(os.path.join(workspace, path_str))
    norm_ws = os.path.realpath(workspace)
    if resolved == norm_ws or resolved.startswith(norm_ws + os.sep):
        return resolved
    if resolved == "/tmp" or resolved.startswith("/tmp/"):
        return resolved
    return None


async def _run_job(job_id: str, shell_cmd: str, workspace: str) -> None:
    """Run a shell command and capture output into _daemon_jobs."""
    try:
        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
        )
        _daemon_jobs[job_id]["proc"] = proc
        out, err = await proc.communicate()
        _daemon_jobs[job_id]["stdout"] = out.decode(errors="replace")
        _daemon_jobs[job_id]["stderr"] = err.decode(errors="replace")
        _daemon_jobs[job_id]["exit_code"] = proc.returncode
    except Exception as exc:
        _daemon_jobs[job_id]["stderr"] = str(exc)
        _daemon_jobs[job_id]["exit_code"] = 1


async def _handle_daemon_command(cmd: dict, workspace: str) -> dict:
    """Route one backend command and return the response payload."""
    action = cmd.get("action", "")
    request_id = cmd.get("request_id", "")
    thread_id = cmd.get("thread_id")
    deployment_id = cmd.get("deployment_id", "")

    def resp(status: str, **kw) -> dict:
        r: dict = {"request_id": request_id, "sandbox_id": deployment_id, "status": status}
        if thread_id:
            r["thread_id"] = thread_id
        rqn = cmd.get("response_queue_name")
        if rqn:
            r["response_queue_name"] = rqn
        r.update(kw)
        return r

    if action == "create_session":
        sid = (cmd.get("payload") or {}).get("session_id") or cmd.get("session_id")
        if not sid:
            return resp("error", error="Missing session_id")
        return resp("success", data={"coding_session_id": sid})

    elif action == "write_code":
        filename = cmd.get("filename")
        code = cmd.get("code")
        workdir = cmd.get("workdir", "")
        if not filename or code is None:
            return resp("error", error="Missing filename or code")
        base = os.path.join(workspace, workdir) if workdir else workspace
        raw = filename if os.path.isabs(filename) else os.path.join(base, filename)
        safe = _safe_resolve(raw, workspace)
        if safe is None:
            return resp("error", error="Path outside workspace/tmp not allowed")
        try:
            os.makedirs(os.path.dirname(safe), exist_ok=True)
            with open(safe, "w") as f:
                f.write(code)
            return resp("success", data={"file_path": safe, "workdir": workdir})
        except OSError as e:
            return resp("error", error=str(e))

    elif action == "get_file":
        file_path = cmd.get("file_path")
        if not file_path:
            return resp("error", error="Missing file_path")
        raw = file_path if os.path.isabs(file_path) else os.path.join(workspace, file_path)
        safe = _safe_resolve(raw, workspace)
        if safe is None:
            return resp("error", error="Path not allowed")
        if not os.path.isfile(safe):
            return resp("error", error="File not found")
        try:
            with open(safe) as f:
                content = f.read()
            return resp("success", data={"file_content": content, "file_path": safe})
        except OSError as e:
            return resp("error", error=str(e))

    elif action == "run_subprocess":
        shell_cmd = cmd.get("command")
        if not shell_cmd:
            return resp("error", error="Missing command")
        job_id = str(uuid.uuid4())
        _daemon_jobs[job_id] = {"stdout": "", "stderr": "", "exit_code": None, "proc": None}
        asyncio.create_task(_run_job(job_id, shell_cmd, workspace))
        return resp("success", data={"job_id": job_id, "detached": True, "message": "Job started"})

    elif action == "get_job_status":
        job_id = cmd.get("job_id")
        if not job_id or job_id not in _daemon_jobs:
            return resp("error", error="Job not found")
        job = _daemon_jobs[job_id]
        done = job["exit_code"] is not None
        return resp("completed" if done else "pending", data={
            "job_id": job_id,
            "stdout": job["stdout"],
            "stderr": job["stderr"],
            "exit_code": job["exit_code"],
            "completed": done,
        })

    elif action == "terminate_job":
        job_id = cmd.get("job_id")
        if not job_id or job_id not in _daemon_jobs:
            return resp("error", error="Job not found")
        proc = _daemon_jobs[job_id].get("proc")
        if proc and _daemon_jobs[job_id]["exit_code"] is None:
            try:
                proc.terminate()
            except Exception:
                pass
        return resp("success", data={"job_id": job_id, "terminated": True})

    elif action == "list_files":
        payload = cmd.get("payload") or {}
        directory = cmd.get("directory") or payload.get("directory") or workspace
        max_depth = cmd.get("max_depth") or payload.get("max_depth") or 10
        include_hidden = cmd.get("include_hidden") or payload.get("include_hidden") or False
        raw = directory if os.path.isabs(directory) else os.path.join(workspace, directory)
        safe = _safe_resolve(raw, workspace)
        if safe is None:
            return resp("error", error="Directory not allowed")
        if not os.path.isdir(safe):
            return resp("error", error="Directory not found")
        _EXCLUDED = {"venv", "node_modules", "env", ".venv"}
        lines: list[str] = []
        base_depth = safe.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(safe, topdown=True):
            depth = dirpath.count(os.sep) - base_depth
            if max_depth > 0 and depth >= max_depth:
                dirnames.clear()
                continue
            if not include_hidden:
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                filenames = sorted(f for f in filenames if not f.startswith("."))
            else:
                dirnames.sort()
                filenames = sorted(filenames)
            for d in dirnames:
                lines.append(f"{os.path.join(dirpath, d)}|d|0")
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED]
            for fname in filenames:
                fp = os.path.join(dirpath, fname)
                try:
                    lines.append(f"{fp}|f|{os.path.getsize(fp)}")
                except OSError:
                    pass
        return resp("success", data={"stdout": "\n".join(lines), "file_count": len(lines), "directory": safe})

    else:
        return resp("error", error=f"Unknown action: {action}")


async def _daemon_poll_loop() -> None:
    """Background coroutine: poll the Neo backend for commands and execute them locally.

    Runs inside the MCP server's asyncio event loop — no Node.js, no VS Code extension needed.
    Generates a stable deployment UUID on first run; subsequent runs reuse it.
    The deployment ID is written to ~/.neo/daemon/daemon.log so _discover_sandbox_id()
    picks it up automatically for neo_submit_task.
    """
    deployment_id = _get_or_create_deployment_id()
    _write_sandbox_log_entry(deployment_id)
    workspace = _server_cwd

    base_interval = 2.0
    max_interval = 60.0
    interval = base_interval
    consecutive_errors = 0

    async with httpx.AsyncClient(base_url=NEO_API_URL, timeout=12.0) as client:
        while True:
            try:
                r = await client.get(
                    f"/v2/poll/{deployment_id}",
                    headers=_headers(),
                    params={"max_messages": 10, "wait_time": 5},
                )
                if r.status_code == 401:
                    await asyncio.sleep(30)
                    continue
                if r.status_code == 404:
                    # Deployment unknown to backend yet — backend learns about it on first submit
                    await asyncio.sleep(30)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")

                data = r.json()
                commands: list[dict] = data if isinstance(data, list) else data.get("messages", [])

                for cmd in commands:
                    response = await _handle_daemon_command(cmd, workspace)
                    try:
                        await client.post(
                            "/v2/poll/response",
                            headers=_headers(),
                            json=response,
                            timeout=10.0,
                        )
                    except Exception:
                        pass  # don't let a send failure stop the loop

                consecutive_errors = 0
                interval = base_interval
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                return
            except Exception:
                consecutive_errors += 1
                interval = min(base_interval * (1.5 ** min(consecutive_errors, 6)), max_interval)
                await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    read_tools = [
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
    ]

    if NEO_READ_ONLY:
        return read_tools

    write_tools = [
        Tool(
            name="neo_submit_task",
            description=(
                "Submit a task to the Neo AI/ML backend. Use this ONLY for AI/ML work: training models, "
                "building AI agents, RAG pipelines, LLM integrations, or ML data pipelines. "
                "For anything outside AI/ML, ask the user first: 'Should I use Neo to build this?' "
                "Returns immediately with a thread_id; background polling tracks progress. "
                "Follow up with neo_task_status, neo_send_feedback (if Neo asks a question), "
                "and neo_get_messages to read the final output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "The task description to send to Neo."},
                    "auto_mode": {
                        "type": "boolean",
                        "description": "Whether to run in auto mode (default: false).",
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
    async with httpx.AsyncClient(base_url=NEO_API_URL, timeout=30.0) as client:

        # ------------------------------------------------------------------ #
        # neo_submit_task — fire-and-forget; polling runs in background       #
        # ------------------------------------------------------------------ #
        if name == "neo_submit_task":
            deployment_id = _get_deployment_id()
            description = arguments["description"]
            auto_mode = arguments.get("auto_mode", False)
            message = f"Working directory: {_server_cwd}\n\nCreate all files inside this directory.\n\n{description}"

            resp = await client.post(
                "/v2/thread/init-chat-direct",
                headers=_headers(),
                json={
                    "message": message,
                    "deployment_type": "vscode",
                    "auto_mode": auto_mode,
                    **({"deployment_id": deployment_id} if deployment_id else {}),
                },
            )
            if resp.status_code != 200:
                # Include actual backend response body to aid debugging
                try:
                    detail = resp.json().get("detail") or resp.json().get("error") or resp.text
                except Exception:
                    detail = resp.text
                return [TextContent(type="text", text=(
                    f"{handle_error(resp.status_code)}\n"
                    f"HTTP {resp.status_code} — {detail}\n"
                    f"deployment_id used: {deployment_id or '(none)'}"
                ))]

            data = resp.json()
            thread_id = (
                data.get("thread_id")
                or data.get("threadId")
                or data.get("id")
            )
            if not thread_id:
                return [TextContent(type="text", text=(
                    f"Backend returned 200 but no thread_id found in response.\n"
                    f"Response: {data}\n"
                    f"deployment_id used: {deployment_id or '(none)'}"
                ))]

            _save_thread_id(thread_id)

            # Start background poller — does not block this response
            asyncio.create_task(_poll_task_bg(thread_id))

            return [TextContent(type="text", text=(
                f"Task submitted. thread_id: {thread_id}\n\n"
                "Polling is running in the background.\n"
                "• neo_task_status — check progress at any time\n"
                "• neo_pause_task  — pause while it's running\n"
                "• neo_send_feedback — reply if it asks a question\n"
                "• neo_stop_task   — cancel and clean up"
            ))]

        # ------------------------------------------------------------------ #
        # neo_task_status — reads in-memory state first, falls back to API   #
        # ------------------------------------------------------------------ #
        elif name == "neo_task_status":
            thread_id, recovered = _resolve_thread_id(arguments)
            if not thread_id:
                return [TextContent(type="text", text="No thread_id provided and no active thread found. Submit a task first.")]

            recovery_note = f"\n(thread_id recovered from storage)" if recovered else ""

            # Fast path: background poller has current state in memory
            if thread_id in _active_polls:
                state = _active_polls[thread_id]
                status = state["status"]
                hints = {
                    "RUNNING": (
                        f"Status: RUNNING. thread_id: {thread_id}{recovery_note}\n"
                        "Background poller is active — call neo_task_status again to refresh."
                    ),
                    "WAITING_FOR_FEEDBACK": (
                        f"Status: WAITING_FOR_FEEDBACK. thread_id: {thread_id}{recovery_note}\n"
                        "Neo has a question. Call neo_send_feedback to reply."
                    ),
                    "PAUSED": (
                        f"Status: PAUSED. thread_id: {thread_id}{recovery_note}\n"
                        "Call neo_resume_task to continue."
                    ),
                    "COMPLETED": (
                        f"Status: COMPLETED. thread_id: {thread_id}{recovery_note}\n"
                        "Call neo_get_messages to read the output."
                    ),
                    "TERMINATED": (
                        f"Status: TERMINATED. thread_id: {thread_id}{recovery_note}\n"
                        "Task was stopped or hit a fatal error."
                    ),
                }
                return [TextContent(type="text", text=hints.get(status, f"Status: {status}. thread_id: {thread_id}{recovery_note}"))]

            # Slow path: no background poller active — hit the API directly
            resp = await client.get(f"/v2/thread/status/{thread_id}", headers=_headers())
            if resp.status_code != 200:
                return [TextContent(type="text", text=handle_error(resp.status_code))]
            status = resp.json().get("status", "UNKNOWN")

            hints = {
                "RUNNING": (
                    f"Status: RUNNING. thread_id: {thread_id}{recovery_note}\n"
                    "No background poller active — call neo_task_status again to refresh."
                ),
                "WAITING_FOR_FEEDBACK": (
                    f"Status: WAITING_FOR_FEEDBACK. thread_id: {thread_id}{recovery_note}\n"
                    "Neo has a question. Call neo_send_feedback to reply."
                ),
                "PAUSED": (
                    f"Status: PAUSED. thread_id: {thread_id}{recovery_note}\n"
                    "Call neo_resume_task to continue."
                ),
                "COMPLETED": (
                    f"Status: COMPLETED. thread_id: {thread_id}{recovery_note}\n"
                    "Call neo_get_messages to read the output."
                ),
                "TERMINATED": (
                    f"Status: TERMINATED. thread_id: {thread_id}{recovery_note}\n"
                    "Task was stopped or hit a fatal error."
                ),
            }
            return [TextContent(type="text", text=hints.get(status, f"Status: {status}. thread_id: {thread_id}{recovery_note}"))]

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

            formatted = [f"[{m.get('role','?').upper()}]\n{m.get('content','')}" for m in msgs]
            output = "\n---\n".join(formatted)
            if capped:
                output += "\n---\n[Output truncated at ~20 000 tokens. Full output available in VS Code.]"
            return [TextContent(type="text", text=output or "No messages found.")]

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
    _check_config()
    asyncio.create_task(_daemon_poll_loop())
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


async def _run_http():
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route
    import uvicorn

    async def handle_mcp(request: Request) -> Response:
        """Single endpoint for all MCP streamable-HTTP traffic (POST / GET / DELETE)."""
        # Extract per-request Neo keys from headers; fall back to env vars
        api_key = request.headers.get("x-access-key", "")
        secret_key = request.headers.get("authorization", "")
        if secret_key.lower().startswith("bearer "):
            secret_key = secret_key[7:]

        if not api_key or not secret_key:
            return JSONResponse(
                {"error": "Missing auth headers. Provide x-access-key and Authorization: Bearer <secret>."},
                status_code=401,
            )

        # Set context vars so _headers() picks them up for this request's async context
        _ctx_api_key.set(api_key)
        _ctx_secret_key.set(secret_key)

        transport = StreamableHTTPServerTransport(mcp_session_id=None, is_json_response_enabled=False)
        async with transport.connect() as (read_stream, write_stream):
            async def run_server():
                await app.run(read_stream, write_stream, app.create_initialization_options())

            import anyio
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_server)
                response = await transport.handle_request(request.scope, request.receive, request._send)  # noqa: SLF001
                tg.cancel_scope.cancel()

        return response

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": "neo-mcp", "transport": "http"})

    starlette_app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/mcp", handle_mcp, methods=["GET", "POST", "DELETE"]),
        ]
    )

    config = uvicorn.Config(starlette_app, host=NEO_HTTP_HOST, port=NEO_HTTP_PORT, log_level="info")
    server = uvicorn.Server(config)
    print(f"Neo MCP HTTP server listening on {NEO_HTTP_HOST}:{NEO_HTTP_PORT}", flush=True)
    asyncio.create_task(_daemon_poll_loop())
    await server.serve()


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from neo_mcp.setup import run_setup
        run_setup(sys.argv[2:])
        return
    try:
        _check_config()
        if NEO_TRANSPORT == "http":
            asyncio.run(_run_http())
        else:
            asyncio.run(_run_stdio())
    except ValueError as e:
        print(f"Neo MCP configuration error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
