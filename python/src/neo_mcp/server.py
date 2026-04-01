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
from datetime import datetime, timezone
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
from .paths import DAEMON_DIR, LOCK_FILE, PID_FILE

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
                    "Submit a task to the Neo ML backend. Returns a thread_id "
                    "immediately. Use neo_task_status to poll for completion."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Full task description for Neo to execute",
                        },
                        "workspace": {
                            "type": "string",
                            "description": "Workspace directory path (defaults to server cwd)",
                        },
                    },
                    "required": ["message"],
                },
            ),
            types.Tool(
                name="neo_task_status",
                description="Get the current status of a Neo task by thread_id.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Thread ID from neo_submit_task",
                        },
                    },
                    "required": ["thread_id"],
                },
            ),
            types.Tool(
                name="neo_get_messages",
                description="Retrieve messages (output) from a Neo task thread.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {"type": "string", "description": "Thread ID"},
                        "before": {
                            "type": "string",
                            "description": "Pagination cursor (older messages)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max messages to return",
                            "default": 50,
                        },
                    },
                    "required": ["thread_id"],
                },
            ),
            types.Tool(
                name="neo_send_feedback",
                description="Send a follow-up message to a running or waiting Neo task.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "thread_id": {"type": "string"},
                        "message": {
                            "type": "string",
                            "description": "Feedback or instruction to send",
                        },
                    },
                    "required": ["thread_id", "message"],
                },
            ),
            types.Tool(
                name="neo_pause_task",
                description="Pause a running Neo task.",
                inputSchema={
                    "type": "object",
                    "properties": {"thread_id": {"type": "string"}},
                    "required": ["thread_id"],
                },
            ),
            types.Tool(
                name="neo_resume_task",
                description="Resume a paused Neo task.",
                inputSchema={
                    "type": "object",
                    "properties": {"thread_id": {"type": "string"}},
                    "required": ["thread_id"],
                },
            ),
            types.Tool(
                name="neo_stop_task",
                description="Stop and clean up a Neo task. This is irreversible.",
                inputSchema={
                    "type": "object",
                    "properties": {"thread_id": {"type": "string"}},
                    "required": ["thread_id"],
                },
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
    if _poller_already_running(deployment_id):
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


def main() -> None:
    """CLI entry point — called by the neo-mcp console script."""
    _setup_logging()

    secret_key = get_secret_key()
    if not secret_key:
        sys.stderr.write(
            "Error: NEO_SECRET_KEY environment variable is not set.\n"
            "Set it before running neo-mcp:\n"
            "  export NEO_SECRET_KEY=sk-v1-...\n"
        )
        sys.exit(1)

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

    anyio.run(run, secret_key, workspace)
