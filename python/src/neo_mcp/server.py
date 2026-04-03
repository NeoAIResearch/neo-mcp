"""stdio MCP server — exposes 7 Neo tools and runs the daemon poller inline.

Architecture:
  - MCP protocol over stdin/stdout (mcp.server.stdio)
  - BackendPoller starts as a background asyncio task alongside the MCP server
  - Lock file (~/.neo/daemon/neo-mcp.lock) prevents duplicate pollers
  - On clean exit (SIGTERM/SIGINT) lock file is removed and poller is cancelled

Tools:
  neo_submit_task   — POST /v2/thread/init-chat-direct
  neo_task_status   — GET  /v2/thread/status/{thread_id}
  neo_get_messages  — GET  /v2/thread/thread-messages
  neo_send_feedback — POST /v2/thread/feedback/{thread_id}
  neo_pause_task    — POST /v2/thread/control/{thread_id} (PAUSE)
  neo_resume_task   — POST /v2/thread/control/{thread_id} (RESUME)
  neo_stop_task     — DELETE /v2/thread/cleanup-direct/{thread_id}
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .action_handlers import ActionHandlers
from .auth import derive_deployment_id, get_secret_key
from .backend_client import BackendClient
from .backend_poller import BackendPoller
from .job_manager import JobManager
from .paths import DAEMON_DIR, LOCK_FILE, PID_FILE, THREAD_WORKSPACES_FILE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lock file helpers
# ---------------------------------------------------------------------------

def _write_lock(pid: int) -> None:
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(
        json.dumps({"pid": pid, "started_at": datetime.now(timezone.utc).isoformat()})
    )

def _remove_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass

def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _poller_already_running(deployment_id: str = "") -> bool:
    """Return True if any daemon with the same deployment ID is already polling.

    Checks (in order):
    1. Our own lock file (neo-mcp.lock)
    2. npm daemon PID file: daemon_{deployment_id}.pid
    3. Generic fallback PID files: npm_daemon.pid / python_daemon.pid
    """
    # 1. Our own lock file
    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text())
            pid = data.get("pid")
            if pid and int(pid) != os.getpid() and _pid_is_alive(int(pid)):
                return True
        except (OSError, ValueError, TypeError):
            pass

    # 2–3. Any other daemon writing a PID file under ~/.neo/daemon/
    candidates = []
    if deployment_id:
        candidates.append(DAEMON_DIR / f"daemon_{deployment_id.replace('-', '')[:8]}.pid")
        candidates.append(DAEMON_DIR / f"daemon_{deployment_id}.pid")
    candidates += [
        DAEMON_DIR / "npm_daemon.pid",
        DAEMON_DIR / "python_daemon.pid",
    ]
    for pid_file in candidates:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if pid != os.getpid() and _pid_is_alive(pid):
                    logger.info("Existing daemon detected via %s (pid=%d) — skipping poller start", pid_file.name, pid)
                    return True
            except (OSError, ValueError):
                pass

    return False


def _load_thread_workspaces() -> dict[str, str]:
    return BackendPoller._load_thread_workspaces()


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

def build_server(
    secret_key: str,
    workspace: str,
) -> tuple[Server, BackendClient, BackendPoller]:
    """Build and wire the MCP server, client, and poller. Returns all three."""
    deployment_id = derive_deployment_id(secret_key)
    thread_workspaces = _load_thread_workspaces()

    client = BackendClient(auth_token=secret_key)
    job_manager = JobManager()
    handlers = ActionHandlers(
        job_manager=job_manager,
        default_workspace=workspace,
        thread_workspaces=thread_workspaces,
    )
    poller = BackendPoller(
        deployment_id=deployment_id,
        client=client,
        handlers=handlers,
        thread_workspaces=thread_workspaces,
    )

    server = Server("neo-mcp")

    # ----------------------------------------------------------------
    # Tool definitions
    # ----------------------------------------------------------------

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="neo_submit_task",
                description=(
                    "Submit an AI/ML task to Neo for local execution. "
                    "Use for: training/fine-tuning models, building AI agents, RAG pipelines, "
                    "LLM integrations, ML data processing. "
                    "NOT for general coding — write that code directly. "
                    "\n\n"
                    "Execution is entirely local: the daemon runs on the user's machine and "
                    "writes files directly to workspace. Files are never stored remotely. "
                    "Neo's output may reference /app/project/src/model.py — the daemon "
                    "automatically remaps this to <workspace>/src/model.py on the local disk. "
                    "\n\n"
                    "Returns {thread_id, status, workspace} immediately. "
                    "Next: neo_task_status to poll, neo_get_messages when COMPLETED."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": (
                                "Full task description. Be specific: state the goal, "
                                "relevant file paths, and constraints. "
                                "Example: 'Train a sentiment classifier on data/reviews.csv, "
                                "save to models/sentiment.pkl, target F1 > 0.85'"
                            ),
                        },
                        "workspace": {
                            "type": "string",
                            "description": (
                                "Absolute path to the PROJECT ROOT — the git repository root "
                                "or top-level project folder. "
                                "ALWAYS infer automatically, never ask the user. "
                                "NEVER pass a subdirectory: if the user is inside "
                                "/home/user/project/src, pass /home/user/project. "
                                "Run `git rev-parse --show-toplevel` to find the git root, "
                                "or fall back to os.getcwd(). "
                                "Passing a subdirectory creates duplicate nested folders."
                            ),
                        },
                    },
                    "required": ["message", "workspace"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="neo_task_status",
                description=(
                    "Get the current status of a Neo task. Returns one of: "
                    "RUNNING (still executing — call again; use neo_task_plan for step details), "
                    "COMPLETED (done — call neo_get_messages for output), "
                    "WAITING_FOR_FEEDBACK (Neo has a question — call neo_send_feedback), "
                    "PAUSED (frozen — call neo_resume_task to continue), "
                    "TERMINATED or FAILED (ended — call neo_get_messages to read what happened). "
                    "\n\n"
                    "Reads from an in-memory cache backed by an adaptive background poller "
                    "(3s–60s). Fast and safe to call once per turn. "
                    "Do NOT poll in a tight loop."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Thread ID from neo_submit_task. Example: 'thread_abc123'",
                        },
                    },
                    "required": ["thread_id"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="neo_get_messages",
                description=(
                    "Retrieve the full conversation output from a completed Neo task. "
                    "Only call when neo_task_status returns COMPLETED — "
                    "for live progress while RUNNING use neo_task_plan instead "
                    "(cheaper, shows per-step status without fetching all messages). "
                    "\n\n"
                    "Output is capped at ~80,000 characters (~20,000 tokens). "
                    "If the response is truncated, paginate backwards using the `before` "
                    "cursor set to the ISO timestamp of the oldest message on the previous page."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Thread ID from neo_submit_task.",
                        },
                        "before": {
                            "type": "string",
                            "description": (
                                "Pagination cursor — ISO 8601 timestamp of the oldest message "
                                "from the previous page. Omit to get the most recent messages first."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max messages to return per page. Default 50, max 200.",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 200,
                        },
                    },
                    "required": ["thread_id"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="neo_send_feedback",
                description=(
                    "Reply to Neo when it is waiting for user input. "
                    "Only call when neo_task_status returns WAITING_FOR_FEEDBACK — "
                    "Neo has paused and needs a decision or clarification before continuing. "
                    "After sending, call neo_task_status again to confirm the task resumed. "
                    "\n\n"
                    "Do NOT use to submit a new task — use neo_submit_task for that."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Thread ID of the task that is WAITING_FOR_FEEDBACK.",
                        },
                        "message": {
                            "type": "string",
                            "description": (
                                "Your reply to Neo's question or additional instructions. "
                                "Example: 'Use PyTorch, target accuracy 90%, save to models/'"
                            ),
                        },
                    },
                    "required": ["thread_id", "message"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="neo_pause_task",
                description=(
                    "Pause a running Neo task mid-execution. "
                    "The task freezes at its current step and can be resumed later with "
                    "neo_resume_task. Safe to call on an already-paused task (no-op). "
                    "To cancel permanently, use neo_stop_task instead."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Thread ID of the running task to pause.",
                        },
                    },
                    "required": ["thread_id"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="neo_resume_task",
                description=(
                    "Resume a paused Neo task from where it stopped. "
                    "Has no effect if the task is already running. "
                    "Only works after neo_pause_task — to start a new task use neo_submit_task."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Thread ID of the paused task to resume.",
                        },
                    },
                    "required": ["thread_id"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="neo_stop_task",
                description=(
                    "Permanently stop and clean up a Neo task. "
                    "IRREVERSIBLE — execution context is deleted and the task cannot be resumed. "
                    "Only call when the user explicitly asks to cancel. "
                    "To pause temporarily (resumable), use neo_pause_task instead."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Thread ID of the task to permanently stop.",
                        },
                    },
                    "required": ["thread_id"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
            ),
            types.Tool(
                name="neo_list_tasks",
                description=(
                    "List all known Neo tasks with their current live status. "
                    "Use this when returning to a session — e.g. after closing and reopening "
                    "Claude Code — to see which tasks are still RUNNING, which are COMPLETED, "
                    "and which need feedback. "
                    "Returns tasks sorted newest-first. For each task: thread_id, workspace, "
                    "status, and last-updated timestamp. "
                    "After getting thread_ids, use neo_task_status or neo_get_messages to "
                    "drill into a specific task."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=True,
                ),
            ),
        ]

    # ----------------------------------------------------------------
    # Tool call handler
    # ----------------------------------------------------------------

    @server.call_tool()
    async def call_tool(
        name: str, arguments: Optional[dict[str, Any]]
    ) -> list[types.TextContent]:
        args = arguments or {}

        try:
            if name == "neo_submit_task":
                result = await _submit_task(client, deployment_id, poller, workspace, args)
            elif name == "neo_task_status":
                result = await _task_status(client, args)
            elif name == "neo_get_messages":
                result = await _get_messages(client, args)
            elif name == "neo_send_feedback":
                result = await _send_feedback(client, args)
            elif name == "neo_pause_task":
                result = await _pause_task(client, args)
            elif name == "neo_resume_task":
                result = await _resume_task(client, args)
            elif name == "neo_stop_task":
                result = await _stop_task(client, poller, args)
            elif name == "neo_list_tasks":
                result = await _list_tasks(client)
            else:
                result = {"error": f"Unknown tool: {name}"}
        except RuntimeError as exc:
            result = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.error("Tool %s failed: %s", name, exc, exc_info=True)
            result = {"error": str(exc)}

        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    return server, client, poller


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _submit_task(
    client: BackendClient,
    deployment_id: str,
    poller: BackendPoller,
    default_workspace: str,
    args: dict,
) -> dict:
    message = args["message"]
    ws = args.get("workspace") or default_workspace
    data = await client.init_chat(message=message, deployment_id=deployment_id, workspace=ws)
    thread_id = data.get("thread_id")
    if thread_id:
        poller.set_thread_status(thread_id, "RUNNING")
        # Register workspace NOW — before any poll commands arrive.
        # ActionHandlers._workspace_for(thread_id) reads this shared dict,
        # so write_code / run_subprocess will use the correct local path.
        poller.register_thread_workspace(thread_id, ws)
    return {"thread_id": thread_id, "status": "submitted", "workspace": ws}


async def _task_status(client: BackendClient, args: dict) -> dict:
    return await client.get_thread_status(args["thread_id"])


async def _get_messages(client: BackendClient, args: dict) -> dict:
    return await client.get_thread_messages(
        thread_id=args["thread_id"],
        before=args.get("before"),
        limit=args.get("limit", 50),
    )


async def _send_feedback(client: BackendClient, args: dict) -> dict:
    await client.send_feedback(thread_id=args["thread_id"], message=args["message"])
    return {"status": "ok", "thread_id": args["thread_id"]}


async def _pause_task(client: BackendClient, args: dict) -> dict:
    await client.control_thread(thread_id=args["thread_id"], signal="PAUSE")
    return {"status": "paused", "thread_id": args["thread_id"]}


async def _resume_task(client: BackendClient, args: dict) -> dict:
    await client.control_thread(thread_id=args["thread_id"], signal="RESUME")
    return {"status": "resumed", "thread_id": args["thread_id"]}


async def _stop_task(
    client: BackendClient, poller: BackendPoller, args: dict
) -> dict:
    thread_id = args["thread_id"]
    await client.stop_thread(thread_id=thread_id)
    poller.set_thread_status(thread_id, "TERMINATED")
    return {"status": "stopped", "thread_id": thread_id}


async def _list_tasks(client: BackendClient) -> dict:
    """Return all known tasks with live status, sorted newest-first.

    Reads thread→workspace from the persisted local file so tasks submitted in
    previous sessions are included — useful for reconnecting after a restart.
    Fetches status for each thread concurrently to keep latency low.
    """
    workspaces = _load_thread_workspaces()
    if not workspaces:
        return {"tasks": [], "count": 0}

    # Load raw file to get updated_at timestamps for sorting
    raw: dict[str, Any] = {}
    if THREAD_WORKSPACES_FILE.exists():
        try:
            raw = json.loads(THREAD_WORKSPACES_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass

    async def fetch_status(thread_id: str, workspace: str) -> dict[str, Any]:
        entry = raw.get(thread_id, {})
        updated_at = entry.get("updated_at", "") if isinstance(entry, dict) else ""
        try:
            status_data = await client.get_thread_status(thread_id)
            status = status_data.get("status", "UNKNOWN")
        except Exception:  # noqa: BLE001
            status = "UNKNOWN"
        return {
            "thread_id": thread_id,
            "workspace": workspace,
            "status": status,
            "updated_at": updated_at,
        }

    results = await asyncio.gather(
        *[fetch_status(tid, ws) for tid, ws in workspaces.items()]
    )
    # Sort newest-first by updated_at (ISO string comparison works correctly)
    tasks = sorted(results, key=lambda t: t["updated_at"], reverse=True)
    return {"tasks": tasks, "count": len(tasks)}


# ---------------------------------------------------------------------------
# Async run + CLI entry point
# ---------------------------------------------------------------------------

async def run(secret_key: str, workspace: str) -> None:
    """Build MCP server, start poller, run stdio transport until EOF."""
    server, client, poller = build_server(secret_key=secret_key, workspace=workspace)

    pid = os.getpid()
    poller_task: Optional[asyncio.Task] = None
    deployment_id = derive_deployment_id(secret_key)

    # Lock file — single poller instance; also yield to Go/npm/extension daemons
    _no_daemon = os.environ.get("NEO_NO_DAEMON", "").lower() in ("1", "true", "yes")
    if _no_daemon:
        logger.info("NEO_NO_DAEMON is set — skipping local poller (bridge/hosted mode)")
    elif _poller_already_running(deployment_id):
        logger.warning(
            "Another daemon is already running for deployment %s. "
            "MCP server will start without a local poller.",
            deployment_id,
        )
    else:
        _write_lock(pid)
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid))
        poller_task = asyncio.create_task(poller.run(), name="backend-poller")
        logger.info(
            "Poller started (pid=%d deployment=%s)",
            pid,
            deployment_id,
        )

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down", signum)
        if poller_task and not poller_task.done():
            poller_task.cancel()
        poller.stop()
        _remove_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if poller_task and not poller_task.done():
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
        _remove_lock()
        logger.info("Server shut down cleanly")


def _setup_logging() -> None:
    """Route all internal logging to a file so it doesn't pollute stdio."""
    log_dir = os.path.expanduser("~/.neo/daemon")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "neo-mcp.log")
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _start_health_server() -> None:
    """Start a minimal HTTP health server on NEO_HEALTH_PORT (default 8080)."""
    port = int(os.environ.get("NEO_HEALTH_PORT", "8080"))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass  # silence access logs

    server = HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health server listening on port %d", port)


def main() -> None:
    """CLI entry point — called by the neo-mcp console script."""
    _setup_logging()
    _start_health_server()

    secret_key = get_secret_key()

    # Workspace: optional first positional arg, or NEO_WORKSPACE_DIR, or cwd
    workspace = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("NEO_WORKSPACE_DIR", os.getcwd())
    )
    workspace = os.path.abspath(workspace)
    if not os.path.isdir(workspace):
        sys.stderr.write(f"Error: workspace '{workspace}' is not a directory.\n")
        sys.exit(1)

    if not secret_key:
        logger.warning("NEO_SECRET_KEY not set — MCP tools unavailable, health endpoint active")
        threading.Event().wait()  # block forever; health server stays up
        return

    anyio.run(run, secret_key, workspace)
