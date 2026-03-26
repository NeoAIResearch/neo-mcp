"""
Neo Python Daemon — standalone local execution backend for neo-mcp.

Replaces the Node.js VS Code extension daemon for pip-only installations.
Polls GET /v2/poll/{deployment_id} for commands and executes them locally,
then sends results back via POST /v2/poll/response.

Authentication:
    Uses the OAuth access_token stored in ~/.neo/daemon/mcp_auth.json.
    Run `neo-mcp login` once to authenticate and write this file.
    The VS Code/Cursor extension also writes this file when logged in.

Supported actions (matches DaemonActionHandlers.ts exactly):
  create_session, write_code, get_file, run_subprocess,
  get_job_status, terminate_job, list_files

Usage:
    neo-mcp daemon [/path/to/workspace] [--deployment-id UUID]

Environment:
    NEO_API_URL      — optional, defaults to https://master.heyneo.so
    NEO_DEPLOYMENT_ID — optional, pin to a specific UUID
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEO_API_URL: str = os.environ.get("NEO_API_URL", "https://master.heyneo.so")
NEO_AUTH_URL: str = os.environ.get("NEO_AUTH_URL", "https://master.heyneo.so")

_DAEMON_DIR = os.path.expanduser("~/.neo/daemon")
_MCP_AUTH_FILE = os.path.join(_DAEMON_DIR, "mcp_auth.json")
_STANDALONE_UUID_FILE = os.path.join(_DAEMON_DIR, "standalone_deployment_id")
_DAEMON_LOG = os.path.join(_DAEMON_DIR, "daemon.log")
_PID_FILE = os.path.join(_DAEMON_DIR, "python_daemon.pid")
_WORKSPACES_FILE = os.path.join(_DAEMON_DIR, "thread-workspaces.json")

# Directories to skip when listing files (matches DaemonActionHandlers.ts)
_SKIP_DIRS = {"venv", "node_modules", "env", ".venv", "__pycache__", ".git", ".tox", "dist", "build"}


# ---------------------------------------------------------------------------
# OAuth token management
# ---------------------------------------------------------------------------

def _load_mcp_auth() -> dict:
    """Load the OAuth credentials written by VS Code extension or neo-mcp login."""
    try:
        return json.loads(Path(_MCP_AUTH_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_mcp_auth(data: dict) -> None:
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    Path(_MCP_AUTH_FILE).write_text(json.dumps(data, indent=2))


def _get_oauth_token() -> str:
    """Return the current OAuth access_token, or empty string if not authenticated."""
    auth = _load_mcp_auth()
    token = auth.get("access_token", "")
    # Treat obviously invalid tokens as missing
    if not token or token in ("\\", "null", "undefined") or len(token) < 10:
        return ""
    return token


async def _refresh_oauth_token() -> str:
    """Try to refresh the OAuth token using the stored refresh_token.
    Returns the new access_token on success, empty string on failure.
    """
    auth = _load_mcp_auth()
    refresh_token = auth.get("refresh_token", "")
    username = auth.get("username", "")
    if not refresh_token or not username:
        return ""
    try:
        async with httpx.AsyncClient(base_url=NEO_AUTH_URL, timeout=10.0) as c:
            r = await c.post(
                "/auth/refresh-token",
                json={"username": username, "refreshToken": refresh_token},
            )
            if r.status_code == 200:
                data = r.json()
                new_token = data.get("token") or data.get("access_token") or data.get("accessToken", "")
                if new_token:
                    auth["access_token"] = new_token
                    _save_mcp_auth(auth)
                    return new_token
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

@dataclass
class _Job:
    proc: asyncio.subprocess.Process  # type: ignore[type-arg]
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    _task: Optional[asyncio.Task] = field(default=None, repr=False)  # type: ignore[type-arg]


_jobs: dict[str, _Job] = {}


# ---------------------------------------------------------------------------
# Deployment ID helpers
# ---------------------------------------------------------------------------

def get_or_create_deployment_id() -> str:
    """Load persisted standalone deployment UUID or generate a new one.
    Matches StateManager.getDeploymentId() from the VS Code extension.
    """
    if env_id := os.environ.get("NEO_DEPLOYMENT_ID"):
        return env_id
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    try:
        uid = Path(_STANDALONE_UUID_FILE).read_text().strip()
        if uid:
            return uid
    except OSError:
        pass
    uid = str(uuid.uuid4())
    Path(_STANDALONE_UUID_FILE).write_text(uid)
    return uid


def write_sandbox_log(deployment_id: str) -> None:
    """Append a sandboxId entry so _discover_sandbox_id() in server.py finds it."""
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    with open(_DAEMON_LOG, "a") as f:
        f.write(f'{{"sandboxId": "{deployment_id}", "source": "python-daemon"}}\n')


def is_running() -> bool:
    """Return True if a Python daemon process is currently alive (via PID file)."""
    try:
        pid = int(Path(_PID_FILE).read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check only
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Thread workspace persistence
# ---------------------------------------------------------------------------

def _load_thread_workspaces() -> dict[str, str]:
    try:
        return json.loads(Path(_WORKSPACES_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_thread_workspaces(workspaces: dict[str, str]) -> None:
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    Path(_WORKSPACES_FILE).write_text(json.dumps(workspaces, indent=2))


# ---------------------------------------------------------------------------
# Backend HTTP helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _poll_backend(
    client: httpx.AsyncClient,
    dep_id: str,
    token: str,
) -> tuple[list[dict], str]:
    """GET /v2/poll/{dep_id} — returns (commands, updated_token).

    Handles 401 by attempting a token refresh once.
    Returns ([], token) on error.
    """
    for attempt in range(2):
        try:
            r = await client.get(
                f"/v2/poll/{dep_id}",
                params={"max_messages": 10, "wait_time": 5},
                headers=_auth(token),
                timeout=15.0,
            )
            if r.status_code == 401 and attempt == 0:
                new_token = await _refresh_oauth_token()
                if new_token:
                    token = new_token
                    continue
                print(
                    "ERROR: OAuth token is invalid or expired. Run 'neo-mcp login' to re-authenticate.",
                    file=sys.stderr,
                    flush=True,
                )
                return [], token
            if r.status_code not in (200, 404):
                return [], token
            if r.status_code == 404:
                return [], token
            data = r.json()
            commands = data if isinstance(data, list) else data.get("messages", [])
            return commands, token
        except Exception:
            return [], token
    return [], token


async def _send_response(
    client: httpx.AsyncClient,
    dep_id: str,
    token: str,
    response: dict,
) -> None:
    """POST /v2/poll/response — send action result back to backend."""
    response.setdefault("sandbox_id", dep_id)
    try:
        await client.post(
            "/v2/poll/response",
            headers=_auth(token),
            json=response,
            timeout=30.0,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Path safety (mirrors DaemonActionHandlers.ts security checks)
# ---------------------------------------------------------------------------

def _safe_resolve(workspace: str, path_str: str) -> Optional[str]:
    """Resolve path and verify it stays within workspace or /tmp.
    Returns None if the path would escape allowed directories.
    """
    if os.path.isabs(path_str):
        resolved = os.path.realpath(path_str)
        allowed = (workspace, "/tmp", "/private/tmp")
        return resolved if any(resolved.startswith(a) for a in allowed) else None
    resolved = os.path.realpath(os.path.join(workspace, path_str))
    return resolved if resolved.startswith(workspace) else None


# ---------------------------------------------------------------------------
# Action handlers — one function per action type
# ---------------------------------------------------------------------------

def _h_create_session(cmd: dict, _ws: str) -> dict:
    sid = (
        cmd.get("session_id")
        or (cmd.get("payload") or {}).get("session_id")
        or str(uuid.uuid4())
    )
    return {
        "request_id": cmd["request_id"],
        "status": "success",
        "data": {"coding_session_id": sid},
    }


def _h_write_code(cmd: dict, workspace: str) -> dict:
    fname = cmd.get("filename")
    code = cmd.get("code")
    if not fname or code is None:
        return {"request_id": cmd["request_id"], "status": "error", "error": "filename and code are required"}
    workdir = cmd.get("workdir") or ""
    base = os.path.join(workspace, workdir) if workdir else workspace
    full = _safe_resolve(base, fname) or _safe_resolve(workspace, fname)
    if not full:
        return {"request_id": cmd["request_id"], "status": "error",
                "error": f"Path escapes allowed directories: {fname}"}
    os.makedirs(os.path.dirname(full), exist_ok=True)
    Path(full).write_text(code, encoding="utf-8")
    return {
        "request_id": cmd["request_id"],
        "status": "success",
        "data": {"file_path": full, "workdir": workdir or workspace},
    }


def _h_get_file(cmd: dict, workspace: str) -> dict:
    fp = cmd.get("file_path")
    if not fp:
        return {"request_id": cmd["request_id"], "status": "error", "error": "file_path is required"}
    full = _safe_resolve(workspace, fp)
    if not full or not os.path.isfile(full):
        return {"request_id": cmd["request_id"], "status": "error", "error": f"File not found: {fp}"}
    content = Path(full).read_text(encoding="utf-8", errors="replace")
    return {
        "request_id": cmd["request_id"],
        "status": "success",
        "data": {"file_content": content, "file_path": full},
    }


async def _h_run_subprocess(cmd: dict, workspace: str) -> dict:
    command_str = cmd.get("command")
    if not command_str:
        return {"request_id": cmd["request_id"], "status": "error", "error": "command is required"}

    job_id = str(uuid.uuid4())
    proc = await asyncio.create_subprocess_shell(
        command_str,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
    )
    job = _Job(proc=proc)
    _jobs[job_id] = job

    async def _collect() -> None:
        stdout_bytes, stderr_bytes = await proc.communicate()
        job.stdout = stdout_bytes.decode(errors="replace")
        job.stderr = stderr_bytes.decode(errors="replace")
        job.exit_code = proc.returncode

    job._task = asyncio.create_task(_collect())
    return {
        "request_id": cmd["request_id"],
        "status": "success",
        "data": {"job_id": job_id, "detached": True, "message": "Job started in background"},
    }


def _h_get_job_status(cmd: dict, _ws: str) -> dict:
    jid = cmd.get("job_id")
    if not jid or jid not in _jobs:
        return {"request_id": cmd["request_id"], "status": "error", "error": f"Job not found: {jid}"}
    job = _jobs[jid]
    done = job.exit_code is not None
    return {
        "request_id": cmd["request_id"],
        "status": "completed" if done else "pending",
        "data": {
            "job_id": jid,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "exit_code": job.exit_code,
            "completed": done,
        },
    }


def _h_terminate_job(cmd: dict, _ws: str) -> dict:
    jid = cmd.get("job_id")
    if not jid or jid not in _jobs:
        return {"request_id": cmd["request_id"], "status": "error", "error": f"Job not found: {jid}"}
    job = _jobs[jid]
    try:
        job.proc.terminate()
    except ProcessLookupError:
        pass
    job.exit_code = -15  # SIGTERM
    job.stderr += "\n[terminated by daemon]"
    return {
        "request_id": cmd["request_id"],
        "status": "success",
        "data": {"job_id": jid, "terminated": True},
    }


def _h_list_files(cmd: dict, workspace: str) -> dict:
    payload = cmd.get("payload") or {}
    directory = cmd.get("directory") or payload.get("directory") or workspace
    max_depth = int(cmd.get("max_depth") or payload.get("max_depth") or 10)
    include_hidden = bool(cmd.get("include_hidden") or payload.get("include_hidden") or False)

    if os.path.isabs(directory):
        target = os.path.realpath(directory)
    else:
        target = _safe_resolve(workspace, directory) or workspace

    if not os.path.isdir(target):
        return {"request_id": cmd["request_id"], "status": "error",
                "error": f"Directory not found: {directory}"}

    lines: list[str] = [f"{target}|d|0"]

    def _walk(path: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except PermissionError:
            return
        for entry in entries:
            if not include_hidden and entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                lines.append(f"{entry.path}|d|0")
                if entry.name not in _SKIP_DIRS:
                    _walk(entry.path, depth + 1)
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{entry.path}|f|{size}")

    _walk(target, 1)
    return {
        "request_id": cmd["request_id"],
        "status": "success",
        "data": {"stdout": "\n".join(lines), "file_count": len(lines), "directory": target},
    }


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

_SYNC_HANDLERS = {
    "create_session": _h_create_session,
    "write_code":     _h_write_code,
    "get_file":       _h_get_file,
    "get_job_status": _h_get_job_status,
    "terminate_job":  _h_terminate_job,
    "list_files":     _h_list_files,
}


async def _dispatch(cmd: dict, workspace: str) -> dict:
    action = cmd.get("action", "")
    rid = cmd.get("request_id", "unknown")
    try:
        if action == "run_subprocess":
            return await _h_run_subprocess(cmd, workspace)
        handler = _SYNC_HANDLERS.get(action)
        if handler:
            return handler(cmd, workspace)
        return {"request_id": rid, "status": "error", "error": f"Unknown action: {action}"}
    except Exception as exc:
        return {"request_id": rid, "status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

async def run_daemon(workspace: Optional[str] = None, deployment_id: Optional[str] = None) -> None:
    ws = os.path.realpath(workspace or os.getcwd())
    dep_id = deployment_id or get_or_create_deployment_id()

    # Load OAuth token from mcp_auth.json (written by VS Code extension or neo-mcp login)
    token = _get_oauth_token()
    if not token:
        print(
            "ERROR: No OAuth token found.\n"
            "Run 'neo-mcp login' to authenticate, or log in via the Neo VS Code/Cursor extension.\n"
            f"Token file: {_MCP_AUTH_FILE}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    # Persist sandbox ID so server.py _discover_sandbox_id() finds this deployment
    write_sandbox_log(dep_id)

    # Write PID file so server.py can check if we're running
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    Path(_PID_FILE).write_text(str(os.getpid()))

    # Load thread→workspace mappings persisted from previous runs
    thread_workspaces: dict[str, str] = _load_thread_workspaces()

    print("Neo daemon ready", flush=True)
    print(f"  deployment_id : {dep_id}", flush=True)
    print(f"  workspace     : {ws}", flush=True)
    print(f"  backend       : {NEO_API_URL}", flush=True)
    print(f"  pid           : {os.getpid()}", flush=True)
    print("Polling for commands...\n", flush=True)

    def _shutdown(signum: int, frame) -> None:  # noqa: ANN001
        print("\nDaemon shutting down.", flush=True)
        try:
            os.unlink(_PID_FILE)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    async with httpx.AsyncClient(base_url=NEO_API_URL) as client:
        backoff = 1.0
        while True:
            commands, token = await _poll_backend(client, dep_id, token)
            if commands:
                backoff = 1.0
                for cmd in commands:
                    tid = cmd.get("thread_id")
                    effective_ws = thread_workspaces.get(tid, ws) if tid else ws
                    resp = await _dispatch(cmd, effective_ws)

                    if tid:
                        resp["thread_id"] = tid
                        if tid not in thread_workspaces:
                            thread_workspaces[tid] = effective_ws
                            _save_thread_workspaces(thread_workspaces)
                    if cmd.get("response_queue_name"):
                        resp["response_queue_name"] = cmd["response_queue_name"]

                    await _send_response(client, dep_id, token, resp)
            else:
                await asyncio.sleep(min(backoff, 10.0))
                backoff = min(backoff * 1.5, 10.0)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog="neo-mcp daemon",
        description=(
            "Neo daemon — polls the Neo backend for commands and executes them locally.\n\n"
            "Requires authentication: run 'neo-mcp login' first, or log in via the\n"
            "Neo VS Code/Cursor extension (it writes the token automatically)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default=None,
        help="Workspace directory where files will be written (default: current directory)",
    )
    parser.add_argument(
        "--deployment-id",
        default=None,
        help="Pin to a specific deployment UUID (default: auto-generated/persisted)",
    )
    args = parser.parse_args()
    asyncio.run(run_daemon(workspace=args.workspace, deployment_id=args.deployment_id))
