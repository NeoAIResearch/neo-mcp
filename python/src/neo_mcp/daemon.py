"""
Neo Python Daemon — standalone local execution backend for neo-mcp.

Replaces the Node.js VS Code extension daemon for pip-only installations.
Polls GET /v2/poll/{deployment_id} for commands and executes them locally,
then sends results back via POST /v2/poll/response.

Authentication:
    Uses NEO_SECRET_KEY as a Bearer token — the same API key used by all
    other neo-mcp requests. No OAuth or browser login needed.

Supported actions (matches DaemonActionHandlers.ts exactly):
  create_session, write_code, get_file, run_subprocess,
  get_job_status, terminate_job, list_files

Usage:
    neo-mcp daemon [/path/to/workspace] [--deployment-id UUID]

Environment:
    NEO_API_URL       — optional, defaults to https://master.heyneo.so
    NEO_SECRET_KEY    — required, API key (sk-v1-...)
    NEO_DEPLOYMENT_ID — optional, pin to a specific UUID
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Environment: "staging" → alpha.heyneo.so, anything else → master.heyneo.so (prod)
_NEO_ENV: str = os.environ.get("NEO_ENV", "prod").lower()
_DEFAULT_URL: str = "https://alpha.heyneo.so" if _NEO_ENV == "staging" else "https://master.heyneo.so"
NEO_API_URL: str = os.environ.get("NEO_API_URL", _DEFAULT_URL)

_DAEMON_DIR = os.path.expanduser("~/.neo/daemon")
_STANDALONE_UUID_FILE = os.path.join(_DAEMON_DIR, "standalone_deployment_id")
_DAEMON_LOG = os.path.join(_DAEMON_DIR, "daemon.log")
_PID_FILE = os.path.join(_DAEMON_DIR, "python_daemon.pid")
_WORKSPACES_FILE = os.path.join(_DAEMON_DIR, "thread-workspaces.json")
_MAX_THREAD_WORKSPACES = int(os.environ.get("NEO_THREAD_WORKSPACES_MAX", "500"))
_THREAD_WORKSPACES_TTL_SECONDS = int(os.environ.get("NEO_THREAD_WORKSPACES_TTL_SECONDS", str(7 * 24 * 60 * 60)))


def _pid_file_for(deployment_id: str) -> str:
    """Return a per-deployment PID file path.

    Using a per-deployment PID file lets multiple daemons (one per user) run
    safely on the same hosted server without clobbering each other's state.
    Short prefix keeps filenames readable.
    """
    return os.path.join(_DAEMON_DIR, f"daemon_{deployment_id[:8]}.pid")

# Directories to skip when listing files (matches DaemonActionHandlers.ts)
_SKIP_DIRS = {"venv", "node_modules", "env", ".venv", "__pycache__", ".git", ".tox", "dist", "build"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_secret_key() -> str:
    """Return the NEO_SECRET_KEY API key used for all requests."""
    return os.environ.get("NEO_SECRET_KEY", "")


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
    """Return the deployment UUID for this daemon instance.

    Priority:
    1. NEO_DEPLOYMENT_ID env var (explicit override)
    2. Derived from NEO_SECRET_KEY — same algorithm as server.py _derive_deployment_id(),
       so the hosted MCP server and this daemon independently compute the same UUID from
       the same API key. No --deployment-id flag or file reading needed.
    3. Persisted UUID from standalone_deployment_id file (legacy / no-key setups)
    4. Generate a random UUID and persist it
    """
    if env_id := os.environ.get("NEO_DEPLOYMENT_ID"):
        return env_id
    if sk := os.environ.get("NEO_SECRET_KEY"):
        import hashlib as _hashlib
        digest = _hashlib.sha256(sk.encode()).digest()[:16]
        return str(uuid.UUID(bytes=digest, version=5))
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


def is_running(deployment_id: str = "") -> bool:
    """Return True if a Python daemon for this deployment is currently alive.

    Checks the per-deployment PID file first (deployment_id given), then falls
    back to the legacy global PID file for backwards compatibility.
    """
    pid_files = []
    if deployment_id:
        pid_files.append(_pid_file_for(deployment_id))
    pid_files.append(_PID_FILE)

    for pid_path in pid_files:
        try:
            pid = int(Path(pid_path).read_text().strip())
            os.kill(pid, 0)  # signal 0 = existence check only
            return True
        except (OSError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------
# Thread workspace persistence
# ---------------------------------------------------------------------------

def _load_thread_workspaces() -> dict[str, str]:
    try:
        data = json.loads(Path(_WORKSPACES_FILE).read_text())
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for tid, value in data.items():
            if isinstance(value, str):
                out[tid] = value
            elif isinstance(value, dict) and isinstance(value.get("workspace"), str):
                out[tid] = value["workspace"]
        return out
    except (OSError, json.JSONDecodeError):
        return {}


def _load_thread_workspaces_meta() -> dict[str, dict[str, int | str]]:
    try:
        data = json.loads(Path(_WORKSPACES_FILE).read_text())
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, int | str]] = {}
        for tid, value in data.items():
            if isinstance(value, dict):
                ws = value.get("workspace")
                ts = value.get("updated_at")
                if isinstance(ws, str) and isinstance(ts, (int, float)):
                    out[tid] = {"workspace": ws, "updated_at": int(ts)}
            elif isinstance(value, str):
                out[tid] = {"workspace": value, "updated_at": int(time.time())}
        return out
    except (OSError, json.JSONDecodeError):
        return {}


def _save_thread_workspaces(workspaces: dict[str, str]) -> None:
    now = int(time.time())
    prev = _load_thread_workspaces_meta()
    entries: dict[str, dict[str, int | str]] = {}
    for tid, ws in workspaces.items():
        if not ws:
            continue
        prev_meta = prev.get(tid)
        if prev_meta and prev_meta.get("workspace") == ws:
            updated_at = int(prev_meta.get("updated_at", now))
        else:
            updated_at = now
        entries[tid] = {"workspace": ws, "updated_at": updated_at}
    min_ts = now - _THREAD_WORKSPACES_TTL_SECONDS
    entries = {
        tid: meta for tid, meta in entries.items()
        if int(meta.get("updated_at", 0)) >= min_ts
    }
    if len(entries) > _MAX_THREAD_WORKSPACES:
        ordered = sorted(entries.items(), key=lambda kv: int(kv[1]["updated_at"]))
        entries = dict(ordered[-_MAX_THREAD_WORKSPACES:])
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    tmp = Path(f"{_WORKSPACES_FILE}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(entries, indent=2))
    os.replace(str(tmp), _WORKSPACES_FILE)


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
    """GET /v2/poll/{dep_id} — returns (commands, token).

    Returns ([], token) on error.
    """
    try:
        r = await client.get(
            f"/v2/poll/{dep_id}",
            params={"max_messages": 10, "wait_time": 5},
            headers=_auth(token),
            timeout=15.0,
        )
        if r.status_code == 401:
            print(
                "ERROR: Auth rejected (401). Check that NEO_SECRET_KEY is set correctly.",
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

    token = _get_secret_key()
    if not token:
        print(
            "ERROR: NEO_SECRET_KEY is not set.\n"
            "Set your API key: export NEO_SECRET_KEY=sk-v1-...",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    # Persist sandbox ID so server.py _discover_sandbox_id() finds this deployment
    write_sandbox_log(dep_id)

    # Write PID files: per-deployment (primary) + legacy global (backwards compat)
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    pid_str = str(os.getpid())
    Path(_pid_file_for(dep_id)).write_text(pid_str)
    Path(_PID_FILE).write_text(pid_str)

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
        for pid_path in (_pid_file_for(dep_id), _PID_FILE):
            try:
                os.unlink(pid_path)
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
            "Requires NEO_SECRET_KEY to be set (export NEO_SECRET_KEY=sk-v1-...)."
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
