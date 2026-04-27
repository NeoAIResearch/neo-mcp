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
import subprocess
import sys
import threading
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .action_handlers import ActionHandlers
from .auth import derive_deployment_id, get_or_create_deployment_id, get_secret_key
from .backend_client import BackendClient
from .backend_poller import BackendPoller
from .integrations import IntegrationManager, PROVIDERS, ValidationError
from .integrations.secret_store import get_secret_store
from .job_manager import JobManager
from .paths import (
    DAEMON_DIR,
    DAEMON_LOG,
    LOCK_FILE,
    PID_FILE,
    STANDALONE_UUID_FILE,
    THREAD_WORKSPACES_FILE,
)


def _package_version() -> str:
    """Return the installed neo-mcp package version, or 'unknown' if undetectable.

    Surfaced in MCP `serverInfo.version` so Inspector / `claude mcp logs` / editor
    tool panels all show the same version users see from `pip show neo-mcp`.
    """
    try:
        from importlib.metadata import version
        return version("neo-mcp")
    except Exception:
        return "unknown"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PyPI update check — fire-and-forget background task
# ---------------------------------------------------------------------------

_PYPI_CACHE_TTL_SECONDS = 24 * 3600
_PYPI_CACHE_FILE = DAEMON_DIR / "pypi_update_check.json"
_PYPI_URL = "https://pypi.org/pypi/neo-mcp/json"


async def _check_for_pypi_update() -> None:
    """Log a stderr WARNING if a newer neo-mcp is on PyPI. Safe to fire-and-forget.

    Never raises — network / parse / cache failures all end silently. Result is
    cached for 24 h in ~/.neo/daemon/pypi_update_check.json so spawns don't
    repeatedly hit PyPI.
    """
    import time

    installed = _package_version()
    if installed == "unknown":
        return

    latest: Optional[str] = None
    try:
        if _PYPI_CACHE_FILE.exists():
            cached = json.loads(_PYPI_CACHE_FILE.read_text())
            if time.time() - float(cached.get("checked_at", 0)) < _PYPI_CACHE_TTL_SECONDS:
                latest = cached.get("latest")
    except Exception:
        pass  # corrupt cache → refetch

    if not latest:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(_PYPI_URL)
                resp.raise_for_status()
                latest = resp.json().get("info", {}).get("version")
            if latest:
                DAEMON_DIR.mkdir(parents=True, exist_ok=True)
                _PYPI_CACHE_FILE.write_text(
                    json.dumps({"checked_at": time.time(), "latest": latest})
                )
        except Exception:
            return  # offline, PyPI flaky, whatever — don't spam logs

    if not latest or latest == installed:
        return

    try:
        from packaging.version import Version
        if Version(latest) <= Version(installed):
            return  # dev build newer than PyPI; don't warn
    except Exception:
        # packaging missing or unparsable — fall back to "different string → warn"
        pass

    logger.warning(
        "neo-mcp %s is installed; %s is available on PyPI. "
        "Upgrade: pip install --upgrade neo-mcp",
        installed, latest,
    )

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


def _deployment_pid_file(deployment_id: str) -> Path:
    return DAEMON_DIR / f"daemon_{deployment_id.replace('-', '')[:8]}.pid"


def _write_deployment_pid(deployment_id: str, pid: int) -> None:
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    _deployment_pid_file(deployment_id).write_text(str(pid))


def _remove_deployment_pid(deployment_id: str) -> None:
    try:
        _deployment_pid_file(deployment_id).unlink(missing_ok=True)
    except OSError:
        pass

def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_cmdline(pid: int) -> Optional[str]:
    """Return the process command line for pid, or None if we can't read it.

    Tries /proc/{pid}/cmdline first (Linux), then `ps -p pid -o args=`
    (macOS/BSD/Windows-WSL). None means "don't know" — caller should fall
    back to the liveness-only check rather than assume dead.
    """
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _pid_is_neo_daemon(pid: int) -> bool:
    """Return True if pid is alive AND its cmdline looks like a neo daemon.

    Guards against PID reuse: a dead daemon's PID can be recycled by the OS
    and handed to an unrelated process (bash, node, etc.). A plain os.kill
    liveness check would then misidentify that unrelated process as a live
    neo daemon and wrongly suppress our in-process poller.

    If we can't read the cmdline at all (unknown platform, permission
    denied), fall back to the liveness-only check to preserve prior behavior.
    """
    if not _pid_is_alive(pid):
        return False
    cmdline = _pid_cmdline(pid)
    if cmdline is None:
        return True  # can't verify — trust liveness
    lowered = cmdline.lower()
    return "neo-mcp" in lowered or "neo_mcp" in lowered


def _poller_already_running(deployment_id: str = "") -> bool:
    """Return True if any daemon with the same deployment ID is already polling.

    Checks (in order):
    1. Our own lock file (neo-mcp.lock)
    2. npm daemon PID file: daemon_{deployment_id}.pid
    3. Generic fallback PID files: npm_daemon.pid / python_daemon.pid

    Stale PID files pointing at reused, non-neo PIDs are deleted so the
    next startup doesn't trip on them again.
    """
    # 1. Our own lock file
    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text())
            pid = data.get("pid")
            if pid and int(pid) != os.getpid():
                if _pid_is_neo_daemon(int(pid)):
                    return True
                if _pid_is_alive(int(pid)) is False:
                    try:
                        LOCK_FILE.unlink(missing_ok=True)
                    except OSError:
                        pass
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
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            continue
        if pid == os.getpid():
            continue
        if _pid_is_neo_daemon(pid):
            logger.info("Existing daemon detected via %s (pid=%d) — skipping poller start", pid_file.name, pid)
            return True
        # Stale: either the PID is dead, or it's been reused by an unrelated
        # process. Remove the file so this MCP start (and future ones) don't
        # suppress our in-process poller.
        logger.info(
            "Stale PID file %s (pid=%d not a neo daemon) — removing",
            pid_file.name, pid,
        )
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    return False


def _load_thread_workspaces() -> dict[str, str]:
    return BackendPoller._load_thread_workspaces()


def _load_thread_wrappers() -> dict[str, list[str]]:
    return BackendPoller._load_thread_wrappers()


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

def build_server(
    secret_key: str,
    workspace: str,
) -> tuple[Server, BackendClient, BackendPoller]:
    """Build and wire the MCP server, client, and poller. Returns all three."""
    deployment_id = get_or_create_deployment_id(secret_key)
    thread_workspaces = _load_thread_workspaces()
    thread_wrappers = _load_thread_wrappers()

    client = BackendClient(auth_token=secret_key)
    job_manager = JobManager()
    handlers = ActionHandlers(
        job_manager=job_manager,
        default_workspace=workspace,
        thread_workspaces=thread_workspaces,
        thread_wrappers=thread_wrappers,
    )
    poller = BackendPoller(
        deployment_id=deployment_id,
        client=client,
        handlers=handlers,
        thread_workspaces=thread_workspaces,
        thread_wrappers=thread_wrappers,
    )

    server = Server("neo-mcp", version=_package_version())

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
                        "wrapper_hint": {
                            "type": "string",
                            "description": (
                                "Optional project slug Neo should treat as the project wrapper "
                                "(e.g. 'rag_system_langchain_0937' or 'kimi-rag-api'). "
                                "Pre-seeds the daemon's wrapper-stripping for this thread, "
                                "eliminating the race where the wrapper is learned only from "
                                "the first absolute container path. Without this, a Neo plan "
                                "that opens with `mkdir -p <slug>/plans` creates a stray "
                                "<slug>/ folder at workspace root. Pass when you know the "
                                "project name in advance."
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
                name="neo_list_integrations",
                description=(
                    "USE THIS TOOL when the user asks questions like \"which keys have "
                    "I saved for Neo?\", \"what integrations are set up?\", \"do I "
                    "already have an OpenRouter/Anthropic/GitHub/HF key configured?\", "
                    "or before calling neo_add_integration to check if a credential "
                    "already exists. "
                    "Returns the provider name, auth method, when it was added, and "
                    "which credential files are registered. NEVER returns the secret "
                    "value itself — safe to show the full response to the user. "
                    "Covers github, huggingface, anthropic, openrouter."
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
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="neo_add_integration",
                description=(
                    "USE THIS TOOL whenever the user wants to give Neo a credential — "
                    "phrasings like \"save my OpenRouter key for Neo\", \"use this "
                    "Anthropic key with Neo\", \"here's my HuggingFace token\", "
                    "\"set my GitHub PAT\", or any message where the user pastes an "
                    "API key / token and wants Neo to use it. "
                    "DO NOT instead suggest the user create a .env file, export an "
                    "env var, or edit a config file — this tool IS the correct and "
                    "only supported way to register credentials for Neo. "
                    "DO NOT ask follow-up questions before calling this tool if the "
                    "user already provided both the provider and the key — just call "
                    "it. "
                    "\n\n"
                    "Key-format heuristics (use when the provider isn't explicit): "
                    "\"sk-or-...\" → openrouter; \"sk-ant-...\" → anthropic; "
                    "\"hf_...\" → huggingface; \"ghp_...\" / \"github_pat_...\" → github. "
                    "\n\n"
                    "Supported providers: github (PAT), huggingface (token), "
                    "anthropic (api_key), openrouter (api_key). Secrets are stored "
                    "locally (native files like ~/.git-credentials or "
                    "~/.neo/integrations/<provider>.env, mode 0o600) and never sent "
                    "to any server. Neo subprocesses automatically inherit them as "
                    "env vars (ANTHROPIC_API_KEY, HF_TOKEN, GITHUB_TOKEN, "
                    "OPENROUTER_API_KEY). "
                    "\n\n"
                    "IMPORTANT — after this tool succeeds, the response contains a "
                    "'safety' string. You MUST relay that safety message to the user "
                    "verbatim so they are reassured the key is stored only on their "
                    "own device and never leaves it. Do NOT echo the credential value."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": sorted(PROVIDERS.keys()),
                            "description": "Which provider to configure.",
                        },
                        "credentials": {
                            "type": "object",
                            "description": (
                                "Provider-specific credential fields. "
                                "github: {pat, username?}; "
                                "huggingface: {token}; "
                                "anthropic: {api_key}; "
                                "openrouter: {api_key}."
                            ),
                            "additionalProperties": True,
                        },
                    },
                    "required": ["provider", "credentials"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="neo_remove_integration",
                description=(
                    "USE THIS TOOL when the user says things like \"remove my "
                    "OpenRouter key from Neo\", \"delete the GitHub integration\", "
                    "\"forget my Anthropic key\", or \"revoke Neo's access to X\". "
                    "Deletes the native credential file(s) and the metadata entry. "
                    "Irreversible — the user must re-supply the secret via "
                    "neo_add_integration to use that provider again. "
                    "If the user wants to REPLACE a key (rotate it), prefer calling "
                    "neo_add_integration directly — it overwrites the existing entry."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": sorted(PROVIDERS.keys()),
                            "description": "Which provider to remove.",
                        },
                    },
                    "required": ["provider"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            types.Tool(
                name="neo_test_integration",
                description=(
                    "USE THIS TOOL when the user says \"check if my OpenRouter key "
                    "still works\", \"test my GitHub token\", \"is my Anthropic key "
                    "valid?\", or right after neo_add_integration to confirm the "
                    "credential is live. Also use it FIRST when a Neo task fails "
                    "with a 401/403/auth error — verify the stored credential before "
                    "debugging the task. "
                    "Calls the provider's own API (GitHub, HuggingFace, Anthropic, "
                    "OpenRouter) and returns {ok, message, latency_ms}. Read-only — "
                    "does not modify the stored credential."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": sorted(PROVIDERS.keys()),
                            "description": "Which provider to test.",
                        },
                    },
                    "required": ["provider"],
                },
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
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
            elif name == "neo_list_integrations":
                result = _list_integrations()
            elif name == "neo_add_integration":
                result = _add_integration(args)
            elif name == "neo_remove_integration":
                result = _remove_integration(args)
            elif name == "neo_test_integration":
                result = await _test_integration(args)
            else:
                result = {"error": f"Unknown tool: {name}"}
        except ValidationError as exc:
            result = {"error": str(exc)}
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

def _workspace_is_mcp_self(workspace: str) -> bool:
    """True if workspace points at the neo-mcp server's own source tree.

    AI agents (Claude Code, etc.) default to `git rev-parse --show-toplevel` which,
    when the user is cd'd inside the neo-mcp repo, resolves to the repo root and
    causes Neo to write project files directly into the MCP source. The distinctive
    marker python/src/neo_mcp/server.py uniquely identifies this repo.
    """
    try:
        marker = Path(workspace).resolve() / "python" / "src" / "neo_mcp" / "server.py"
        return marker.is_file()
    except OSError:
        return False


def _normalize_workspace(ws: str) -> str:
    """Canonicalize a workspace path.

    Collapses `.`, `..`, duplicate slashes, trailing slashes, `~`, and
    resolves symlinks so that `/foo/`, `/foo`, and `/foo/./` all produce
    the same key in thread-workspaces.json.
    """
    return str(Path(ws).expanduser().resolve())


def _validate_workspace(ws: str) -> Optional[str]:
    """Return a user-facing error string if ws is unusable, else None.

    Runs *after* normalization so error messages show the canonical path.
    Fails fast at submit time instead of deferring to cryptic write errors.
    """
    p = Path(ws)
    if not p.is_absolute():
        return (
            f"Workspace must be an absolute path, got {ws!r}. "
            f"Pass the full project-root path (e.g. /home/user/myproject)."
        )
    if not p.exists():
        return (
            f"Workspace {ws!r} does not exist. "
            f"Create the directory first, or pass an existing project root."
        )
    if not p.is_dir():
        return f"Workspace {ws!r} is not a directory."
    if not os.access(ws, os.W_OK):
        return (
            f"Workspace {ws!r} is not writable by this process "
            f"(uid={os.geteuid()}). Check directory permissions."
        )
    return None


async def _submit_task(
    client: BackendClient,
    deployment_id: str,
    poller: BackendPoller,
    default_workspace: str,
    args: dict,
) -> dict:
    message = args["message"]
    ws_raw = args.get("workspace") or default_workspace
    # Reject relative paths *before* normalization, otherwise Path.resolve()
    # silently prepends the MCP server's cwd and the user gets a misleading
    # "does not exist" error instead of learning their input was wrong.
    # Tilde and absolute paths fall through to normalization.
    if not ws_raw.startswith("~") and not os.path.isabs(ws_raw):
        return {
            "error": (
                f"Workspace must be an absolute path, got {ws_raw!r}. "
                f"Pass the full project-root path (e.g. /home/user/myproject) "
                f"or a tilde-prefixed path like ~/myproject."
            )
        }
    # Normalize so subsequent checks, the mcp-self guard, and the
    # thread-workspaces.json key all agree on a single canonical path.
    try:
        ws = _normalize_workspace(ws_raw)
    except OSError as exc:
        return {"error": f"Could not resolve workspace {ws_raw!r}: {exc}"}
    if _workspace_is_mcp_self(ws):
        return {
            "error": (
                f"Refusing workspace {ws!r} — this is the neo-mcp server's own source tree. "
                f"Neo would write task files directly into the MCP source code. "
                f"Pick a different workspace: create a project folder (e.g. "
                f"{os.path.join(ws, 'my_project')}) and resubmit with that path."
            )
        }
    validation_error = _validate_workspace(ws)
    if validation_error is not None:
        return {"error": validation_error}
    wrapper_hint = args.get("wrapper_hint") or None
    if isinstance(wrapper_hint, str):
        wrapper_hint = wrapper_hint.strip() or None
    data = await client.init_chat(
        message=message,
        deployment_id=deployment_id,
        workspace=ws,
        wrapper_hint=wrapper_hint,
    )
    thread_id = data.get("thread_id")
    if thread_id:
        poller.set_thread_status(thread_id, "RUNNING")
        # Register wrapper FIRST so the very first command — even a relative
        # `mkdir -p <slug>/...` that lands before any absolute path teaches
        # the daemon the slug — gets stripped instead of creating a stray
        # <slug>/ folder at workspace root.
        if wrapper_hint:
            poller.register_thread_wrapper(thread_id, wrapper_hint)
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
    # Evict from thread-workspaces.json + in-memory caches so stopped threads
    # don't accumulate across sessions. Status cache is cleared inside
    # forget_thread, so set_thread_status here would be redundant.
    poller.forget_thread(thread_id)
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
# Integration tool implementations
# ---------------------------------------------------------------------------

def _list_integrations() -> dict:
    mgr = IntegrationManager()
    items = mgr.list()
    # Count entries present in the shared metadata file but NOT usable by
    # this server (e.g. VS Code extension entries keyed by random IDs).
    # Surfacing this lets the user understand why neo_list_integrations
    # might show fewer rows than another tool inspecting the same file.
    raw = mgr._load_metadata().get("integrations", {})
    ignored = max(0, len(raw) - len(items))
    resp: dict = {"count": len(items), "integrations": items}
    if ignored:
        resp["ignored_foreign_entries"] = ignored
        resp["note"] = (
            f"{ignored} entr{'y' if ignored == 1 else 'ies'} in "
            "~/.neo/integrations.json were written by another tool (likely "
            "the Neo VS Code extension) under non-canonical keys — Neo tasks "
            "started from this server cannot load those credentials. Re-add "
            "them via neo_add_integration if you want this server to use them."
        )
    return resp


_ASSISTANT_RELAY_INSTRUCTION = (
    "TELL THE USER: relay the 'safety' message above to the user verbatim, "
    "so they know exactly where their credentials live and what guarantees "
    "apply. Do not show the credential value back to the user."
)

_SAFETY_COMMON_TAIL = (
    "They never leave your machine — never sent to Neo's backend, never "
    "logged, never committed to git (~/.neo/ and .env are in .gitignore). "
    "Neo injects them as environment variables only when running your "
    "tasks locally on this computer. Run neo_remove_integration to delete them."
)


def _build_safety_message(provider: str, backend: str, files_written: list[str]) -> str:
    """Produce a truthful per-storage-site reassurance string.

    Providers can write more than one location:
      - anthropic/openrouter: one location (either keyring entry or 0o600 .env)
      - huggingface/github:   two locations (the canonical store + a native
                              plaintext file at 0o600 so the matching CLI
                              tool can read it directly)

    We enumerate every location so the safety message is precisely accurate.
    Saying "only in keyring" when we ALSO wrote ~/.cache/huggingface/token
    would be a lie the user might rely on.
    """
    keyring_entries = [f for f in files_written if f.startswith("neo-mcp:")]
    file_paths = [f for f in files_written if not f.startswith("neo-mcp:")]

    lead = f"Your {provider} credentials are stored only on this device."

    if keyring_entries and file_paths:
        where = (
            f"The primary copy lives in your operating-system keyring "
            f"({backend}, encrypted at rest). A second copy is written at "
            f"{', '.join(file_paths)} (mode 0o600, owner-readable only) so "
            f"the matching CLI tool (git / huggingface-cli) can read it directly."
        )
    elif keyring_entries:
        where = (
            f"Held in your operating-system keyring ({backend}), "
            f"encrypted at rest."
        )
    else:
        where = (
            f"Written to {', '.join(file_paths)} with mode 0o600 "
            f"(owner-readable only)."
        )

    return " ".join((lead, where, _SAFETY_COMMON_TAIL))


def _add_integration(args: dict) -> dict:
    provider = args.get("provider")
    credentials = args.get("credentials") or {}
    if not provider:
        raise ValidationError("Missing required field: provider")
    if not isinstance(credentials, dict):
        raise ValidationError("credentials must be an object")
    mgr = IntegrationManager()
    result = mgr.add(provider, credentials)

    store = get_secret_store()
    backend = store.backend
    files_written = result.get("files_written", [])
    # storage_location stays for backwards-compatibility callers — first
    # entry in files_written is the canonical primary location.
    location = files_written[0] if files_written else store.location(provider)

    return {
        "status": "added",
        "provider": provider,
        "storage_backend": backend,
        "storage_location": location,
        "files_written": files_written,
        "safety": _build_safety_message(provider, backend, files_written),
        "assistant_instruction": _ASSISTANT_RELAY_INSTRUCTION,
    }


def _remove_integration(args: dict) -> dict:
    provider = args.get("provider")
    if not provider:
        raise ValidationError("Missing required field: provider")
    mgr = IntegrationManager()
    result = mgr.remove(provider)
    return {"status": "removed", **result}


async def _test_integration(args: dict) -> dict:
    provider = args.get("provider")
    if not provider:
        raise ValidationError("Missing required field: provider")
    mgr = IntegrationManager()
    return await mgr.test(provider)


# ---------------------------------------------------------------------------
# Async run + CLI entry point
# ---------------------------------------------------------------------------

async def run(secret_key: str, workspace: str) -> None:
    """Build MCP server, start poller, run stdio transport until EOF."""
    logger.info("neo-mcp %s starting (workspace=%s)", _package_version(), workspace)
    asyncio.create_task(_check_for_pypi_update(), name="pypi-update-check")

    server, client, poller = build_server(secret_key=secret_key, workspace=workspace)

    pid = os.getpid()
    poller_task: Optional[asyncio.Task] = None
    deployment_id = get_or_create_deployment_id(secret_key)
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
        _write_deployment_pid(deployment_id, pid)
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
        _remove_deployment_pid(deployment_id)
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
        await client.aclose()
        _remove_deployment_pid(deployment_id)
        _remove_lock()
        logger.info("Server shut down cleanly")


def _setup_logging() -> None:
    """Route all internal logging to a file so it doesn't pollute stdio."""
    log_dir = os.path.expanduser("~/.neo/daemon")
    log_file = os.path.join(log_dir, "neo-mcp.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    except OSError:
        # Fallback for read-only environments (tests/sandboxes): keep CLI usable.
        logging.basicConfig(
            stream=sys.stderr,
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

    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
    except OSError:
        logger.warning("Health server port %d already in use — skipping health endpoint", port)
        return
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health server listening on port %d", port)


def _deployment_id_source(secret_key: str) -> tuple[str, str]:
    explicit = os.environ.get("NEO_DEPLOYMENT_ID", "").strip()
    if explicit:
        return explicit, "explicit-env"
    mode = os.environ.get("NEO_DEPLOYMENT_ID_MODE", "").strip().lower()
    if mode in {"key-derived", "key", "deterministic"} and secret_key:
        return derive_deployment_id(secret_key), "key-derived-mode"
    if STANDALONE_UUID_FILE.exists():
        persisted = STANDALONE_UUID_FILE.read_text().strip()
        if persisted:
            return persisted, "machine-persisted"
    return "", "unset"


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def _cmd_status(json_mode: bool = False) -> int:
    secret_key = get_secret_key() or ""
    dep_id, source = _deployment_id_source(secret_key)
    daemon_pid = None
    daemon_running = False
    if dep_id:
        pid_file = _deployment_pid_file(dep_id)
        if pid_file.exists():
            try:
                daemon_pid = int(pid_file.read_text().strip())
                daemon_running = _pid_is_alive(daemon_pid)
            except (OSError, ValueError):
                daemon_pid = None
                daemon_running = False

    thread_count = 0
    if THREAD_WORKSPACES_FILE.exists():
        try:
            data = json.loads(THREAD_WORKSPACES_FILE.read_text())
            if isinstance(data, dict):
                thread_count = len(data)
        except Exception:  # noqa: BLE001
            thread_count = 0

    payload = {
        "mode": "stdio-daemon-first",
        "http_mode": "obsolete-not-used",
        "secret_key_present": bool(secret_key),
        "deployment_id": dep_id,
        "deployment_id_source": source,
        "daemon_running": daemon_running,
        "daemon_pid": daemon_pid,
        "thread_mappings": thread_count,
        "daemon_dir": str(DAEMON_DIR),
    }
    if json_mode:
        _json_print(payload)
    else:
        print("Neo MCP status")
        print(f"  mode:                 {payload['mode']}")
        print(f"  http_mode:            {payload['http_mode']}")
        print(f"  secret_key_present:   {payload['secret_key_present']}")
        print(f"  deployment_id:        {dep_id or '(none)'}")
        print(f"  deployment_id_source: {source}")
        print(f"  daemon_running:       {daemon_running}")
        print(f"  daemon_pid:           {daemon_pid if daemon_pid is not None else '(none)'}")
        print(f"  thread_mappings:      {thread_count}")
        print(f"  daemon_dir:           {DAEMON_DIR}")
    return 0


def _cmd_doctor(json_mode: bool = False) -> int:
    checks: list[dict[str, Any]] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append({
        "name": "python_version>=3.11",
        "ok": py_ok,
        "detail": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    })

    key = get_secret_key() or ""
    checks.append({
        "name": "secret_key_present",
        "ok": bool(key),
        "detail": "NEO_SECRET_KEY set" if key else "NEO_SECRET_KEY missing",
    })

    try:
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(DAEMON_DIR, os.W_OK)
    except OSError:
        writable = False
    checks.append({
        "name": "daemon_dir_writable",
        "ok": writable,
        "detail": str(DAEMON_DIR),
    })

    dep_id, source = _deployment_id_source(key)
    checks.append({
        "name": "deployment_id_resolved",
        "ok": bool(dep_id),
        "detail": f"{dep_id or 'none'} ({source})",
    })

    daemon_ok = False
    if dep_id:
        pf = _deployment_pid_file(dep_id)
        if pf.exists():
            try:
                daemon_ok = _pid_is_alive(int(pf.read_text().strip()))
            except (OSError, ValueError):
                daemon_ok = False
    checks.append({
        "name": "daemon_running_for_deployment",
        "ok": daemon_ok,
        "detail": dep_id or "no deployment id",
    })

    payload = {
        "mode": "stdio-daemon-first",
        "http_mode": "obsolete-not-used",
        "all_ok": all(c["ok"] for c in checks),
        "checks": checks,
        "hints": [
            "Set NEO_SECRET_KEY=sk-v1-... if missing",
            "Run `neo-mcp daemon` in a separate terminal for local execution",
            "Use default machine deployment ID unless you explicitly need deterministic key-derived mode",
        ],
    }
    if json_mode:
        _json_print(payload)
    else:
        print("Neo MCP doctor")
        for c in checks:
            mark = "OK" if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']}: {c['detail']}")
        print("\nNotes:")
        print("  - HTTP mode is obsolete; this tool validates stdio+local daemon flow.")
        for h in payload["hints"]:
            print(f"  - {h}")
    return 0 if payload["all_ok"] else 1


def _cmd_logs(lines: int = 120, source: str = "neo-mcp") -> int:
    file_map = {
        "neo-mcp": DAEMON_DIR / "neo-mcp.log",
        "daemon": DAEMON_LOG,
    }
    target = file_map.get(source, DAEMON_DIR / "neo-mcp.log")
    if not target.exists():
        print(f"No log file found: {target}")
        return 1
    data = target.read_text(errors="replace").splitlines()
    for line in data[-max(lines, 1):]:
        print(line)
    return 0


def _cmd_tail(lines: int = 120, source: str = "neo-mcp") -> int:
    """Shortcut alias for logs tail output."""
    return _cmd_logs(lines=lines, source=source)


def _cmd_list(json_mode: bool = False) -> int:
    if not THREAD_WORKSPACES_FILE.exists():
        payload = {"count": 0, "tasks": []}
        if json_mode:
            _json_print(payload)
        else:
            print("No known tasks yet.")
        return 0

    raw = json.loads(THREAD_WORKSPACES_FILE.read_text() or "{}")
    tasks: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for tid, value in raw.items():
            if isinstance(value, str):
                tasks.append({"thread_id": tid, "workspace": value, "updated_at": ""})
            elif isinstance(value, dict):
                tasks.append({
                    "thread_id": tid,
                    "workspace": value.get("workspace", ""),
                    "updated_at": value.get("updated_at", ""),
                })
    tasks.sort(key=lambda t: str(t.get("updated_at", "")), reverse=True)
    payload = {"count": len(tasks), "tasks": tasks}
    if json_mode:
        _json_print(payload)
    else:
        if not tasks:
            print("No known tasks yet.")
            return 0
        print("Known tasks")
        for t in tasks:
            print(f"  - {t['thread_id']}  {t['workspace']}  updated_at={t['updated_at']}")
    return 0


def _cmd_self_test(json_mode: bool = False) -> int:
    checks: list[dict[str, Any]] = []
    td = tempfile.mkdtemp(prefix="neo-selftest-")
    ws = os.path.join(td, "ws")
    os.makedirs(ws, exist_ok=True)
    jm = JobManager()
    handlers = ActionHandlers(jm, ws, {})

    async def _run_local_checks() -> tuple[bool, bool]:
        good = await handlers.handle_command({
            "action": "write_code",
            "request_id": "self-1",
            "filename": "a.py",
            "code": "print('ok')",
        })
        bad = await handlers.handle_command({
            "action": "write_code",
            "request_id": "self-2",
            "filename": "../../../../etc/passwd",
            "code": "x",
        })
        return good.get("status") == "success", bad.get("status") == "error"

    ok_write, ok_block = asyncio.run(_run_local_checks())
    checks.append({"name": "write_code_within_workspace", "ok": ok_write})
    checks.append({"name": "path_traversal_blocked", "ok": ok_block})
    checks.append({"name": "http_mode_disabled", "ok": True, "detail": "stdio/daemon-only validation"})

    payload = {"all_ok": all(c["ok"] for c in checks), "checks": checks}
    if json_mode:
        _json_print(payload)
    else:
        print("Neo MCP self-test")
        for c in checks:
            mark = "OK" if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']}")
        print("  This test validates local daemon semantics only.")
    return 0 if payload["all_ok"] else 1


async def run_daemon(secret_key: str, workspace: str, deployment_id: Optional[str] = None) -> None:
    dep = deployment_id or get_or_create_deployment_id(secret_key)
    if _poller_already_running(dep):
        logger.warning("Another daemon is already running for deployment %s", dep)
        return

    thread_workspaces = _load_thread_workspaces()
    thread_wrappers = _load_thread_wrappers()
    client = BackendClient(auth_token=secret_key)
    handlers = ActionHandlers(
        job_manager=JobManager(),
        default_workspace=workspace,
        thread_workspaces=thread_workspaces,
        thread_wrappers=thread_wrappers,
    )
    poller = BackendPoller(
        deployment_id=dep,
        client=client,
        handlers=handlers,
        thread_workspaces=thread_workspaces,
        thread_wrappers=thread_wrappers,
    )

    pid = os.getpid()
    _write_lock(pid)
    PID_FILE.write_text(str(pid))
    _write_deployment_pid(dep, pid)
    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Daemon received signal %d — shutting down", signum)
        poller.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    task = asyncio.create_task(poller.run(), name="backend-poller-daemon")
    try:
        await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await client.aclose()
        _remove_deployment_pid(dep)
        _remove_lock()


def _install_skill_silently() -> None:
    """Copy the bundled Claude Code skill to ~/.claude/skills/neo.md (best-effort, silent)."""
    try:
        import pathlib, shutil
        skill_src = pathlib.Path(__file__).parent / "skills" / "neo.md"
        if not skill_src.exists():
            return
        skills_dir = pathlib.Path.home() / ".claude" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_src, skills_dir / "neo.md")
    except Exception:
        pass


def main() -> None:
    """CLI entry point — called by the neo-mcp console script."""
    _setup_logging()
    _start_health_server()
    _install_skill_silently()

    args = sys.argv[1:]
    known = {"setup", "doctor", "status", "list", "logs", "tail", "self-test", "daemon"}

    if args and args[0] == "setup":
        from .setup import run_setup
        run_setup(args[1:])
        return

    if args and args[0] == "doctor":
        json_mode = "--json" in args[1:]
        sys.exit(_cmd_doctor(json_mode=json_mode))

    if args and args[0] == "status":
        json_mode = "--json" in args[1:]
        sys.exit(_cmd_status(json_mode=json_mode))

    if args and args[0] == "list":
        json_mode = "--json" in args[1:]
        sys.exit(_cmd_list(json_mode=json_mode))

    if args and args[0] == "logs":
        lines = 120
        source = "neo-mcp"
        tail_args = args[1:]
        for i, a in enumerate(tail_args):
            if a == "--lines" and i + 1 < len(tail_args):
                try:
                    lines = int(tail_args[i + 1])
                except ValueError:
                    pass
            if a == "--source" and i + 1 < len(tail_args):
                source = tail_args[i + 1]
        sys.exit(_cmd_logs(lines=lines, source=source))

    if args and args[0] == "tail":
        lines = 120
        source = "neo-mcp"
        tail_args = args[1:]
        for i, a in enumerate(tail_args):
            if a == "--lines" and i + 1 < len(tail_args):
                try:
                    lines = int(tail_args[i + 1])
                except ValueError:
                    pass
            if a == "--source" and i + 1 < len(tail_args):
                source = tail_args[i + 1]
        sys.exit(_cmd_tail(lines=lines, source=source))

    if args and args[0] == "self-test":
        json_mode = "--json" in args[1:]
        sys.exit(_cmd_self_test(json_mode=json_mode))

    secret_key = get_secret_key()

    if args and args[0] == "daemon":
        if not secret_key:
            sys.stderr.write("Error: NEO_SECRET_KEY is required for daemon mode.\n")
            sys.exit(1)

        dep_override: Optional[str] = None
        workspace_arg: Optional[str] = None
        daemon_args = args[1:]
        i = 0
        while i < len(daemon_args):
            cur = daemon_args[i]
            if cur == "--deployment-id" and i + 1 < len(daemon_args):
                dep_override = daemon_args[i + 1]
                i += 2
                continue
            if not cur.startswith("-") and workspace_arg is None:
                workspace_arg = cur
            i += 1
        workspace = os.path.abspath(workspace_arg or os.environ.get("NEO_WORKSPACE_DIR", os.getcwd()))
        if not os.path.isdir(workspace):
            sys.stderr.write(f"Error: workspace '{workspace}' is not a directory.\n")
            sys.exit(1)
        anyio.run(run_daemon, secret_key, workspace, dep_override)
        return

    # Default mode: stdio MCP server (legacy behavior preserved).
    # First non-command positional argument is treated as workspace.
    workspace_arg = None
    if args and args[0] not in known:
        workspace_arg = args[0]
    workspace = os.path.abspath(workspace_arg or os.environ.get("NEO_WORKSPACE_DIR", os.getcwd()))
    if not os.path.isdir(workspace):
        sys.stderr.write(f"Error: workspace '{workspace}' is not a directory.\n")
        sys.exit(1)

    if not secret_key:
        logger.warning("NEO_SECRET_KEY not set — MCP tools unavailable, health endpoint active")
        threading.Event().wait()  # block forever; health server stays up
        return

    anyio.run(run, secret_key, workspace)
