"""Daemon supervision: detached spawn, systemd install, and teardown.

Design (see CLAUDE.md / the "dev-trustworthy pip daemon" rationale):

  * The backend poll loop must NEVER run inside the MCP stdio process that an
    editor (Claude Code, Cursor, …) spawns: that process is a child of the
    interactive client, shares its controlling terminal + process group, and
    is therefore suspended (SIGTSTP) or killed whenever the editor is Ctrl-Z'd
    or its SSH session drops. Polling must live in a *detached* daemon.

  * Two ways to run that daemon, in order of trustworthiness:
      1. ``neo-mcp install-service`` — a systemd **user** unit (Restart=always,
         no controlling TTY, secret in a 0600 EnvironmentFile). Explicit,
         visible in ``systemctl --user status``, survives reboot, clean
         teardown. Preferred for servers / long-lived sandboxes.
      2. Auto-spawn fallback — ``spawn_detached_daemon`` uses
         ``start_new_session=True`` + ``stdio=DEVNULL`` (the POSIX equivalent
         of the VS Code extension's ``detached:true, stdio:'ignore'``) so the
         daemon escapes the editor's session/process group. Used when no
         service is installed.

  * ``pip uninstall`` has no reliable pre-uninstall hook, so teardown is the
    explicit ``neo-mcp uninstall`` command (mirrors the extension's
    deactivate/cleanup): stop the daemon, remove lock/pid/uuid, strip the
    ``neo`` MCP entries this package added, and remove the installed skill.

stdlib only — importable from both the CLI and the stdio server. This module
must not import ``server`` at module load (avoids a circular import); callers
pass in the resolved ``deployment_id``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .paths import (
    DAEMON_DIR,
    JOBS_FILE,
    JOBS_LOG_DIR,
    LOCK_FILE,
    NEO_DIR,
    PID_FILE,
    THREAD_WORKSPACES_FILE,
    deployment_ready_file,
)

# Ready-file heartbeat older than this is not a live poller (park tick is 2s).
HEARTBEAT_STALE_SECONDS = 10.0

_ENV_FILE = DAEMON_DIR / "neo-mcp.env"
_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
_UNIT_NAME = "neo-mcp.service"
_UNIT_PATH = _SYSTEMD_USER_DIR / _UNIT_NAME
_SKILL_FILE = Path.home() / ".claude" / "skills" / "neo.md"


# ---------------------------------------------------------------------------
# PID / identity helpers (single copy — server.py must not duplicate these)
# ---------------------------------------------------------------------------

def package_version() -> str:
    """Installed neo-mcp version, or 'unknown' if metadata is missing."""
    try:
        from importlib.metadata import version
        return version("neo-mcp")
    except Exception:
        return "unknown"


def _deployment_pid_file(deployment_id: str) -> Path:
    return DAEMON_DIR / f"daemon_{deployment_id.replace('-', '')[:8]}.pid"


def identity_payload(pid: int, deployment_id: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pid": pid,
        "impl": "python",
        "version": package_version(),
        "executable": sys.executable,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if deployment_id:
        payload["deployment_id"] = deployment_id
    return payload


def write_identity(path: Path, pid: int, deployment_id: str = "") -> None:
    """Write the daemon self-report document (VS Code /health analogue on disk)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity_payload(pid, deployment_id)) + "\n")


def read_identity(path: Path) -> dict[str, Any]:
    """Parse a pid/lock document. A bare integer is foreign/legacy (no version)."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("pid") is not None:
            return data
        if isinstance(data, int):
            return {"pid": data}
    except json.JSONDecodeError:
        pass
    try:
        return {"pid": int(raw)}
    except ValueError:
        return {}


def _pid_from_identity(data: dict[str, Any]) -> Optional[int]:
    try:
        pid = int(data["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _pid_cmdline(pid: int) -> Optional[str]:
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():
        try:
            return proc_path.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            ).strip()
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _pid_matches_daemon(pid: int, deployment_id: str = "") -> bool:
    """Fail closed unless PID identity matches a neo daemon command."""
    if not _pid_alive(pid):
        return False
    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return False
    lowered = cmdline.lower()
    is_neo = "neo-mcp" in lowered or "neo_mcp" in lowered
    is_daemon = (
        " daemon" in lowered
        or "neo-mcp-daemon" in lowered
        or "-m neo_mcp" in lowered
    )
    if not is_neo or not is_daemon:
        return False
    if deployment_id and deployment_id not in cmdline and "neo-mcp-daemon" not in lowered:
        return False
    return True


def running_daemon_pids(deployment_id: str = "") -> list[int]:
    """Collect candidate daemon PIDs after strict identity validation."""
    pids: set[int] = set()
    for path in _identity_paths(deployment_id):
        pid = _pid_from_identity(read_identity(path))
        if pid:
            pids.add(pid)
    return [p for p in pids if _pid_matches_daemon(p, deployment_id)]


def _identity_paths(deployment_id: str = "") -> list[Path]:
    paths = [PID_FILE, LOCK_FILE]
    if deployment_id:
        paths.append(_deployment_pid_file(deployment_id))
        paths.append(DAEMON_DIR / f"daemon_{deployment_id}.pid")
    paths += [DAEMON_DIR / "npm_daemon.pid", DAEMON_DIR / "python_daemon.pid"]
    return paths


def probe_daemon(deployment_id: str = "") -> dict[str, Any]:
    """Identity from pid/lock files; capability from a fresh ready-file heartbeat.

    ``liveness_ok`` — a live neo daemon pid for this deployment.
    ``identity_ok`` — that pid is *this* Python install (version + executable).
    Missing version means foreign/legacy, not stale.
    ``heartbeat_ok`` — ready file names this deployment and ``observed_at`` is fresh.
    ``ok`` is capability (``heartbeat_ok``): any impl that heartbeats is current.
    """
    installed_version = package_version()
    result: dict[str, Any] = {
        "ok": False,
        "liveness_ok": False,
        "identity_ok": False,
        "heartbeat_ok": False,
        "pid": None,
        "impl": None,
        "version": None,
        "executable": None,
        "installed_version": installed_version,
        "installed_executable": sys.executable,
        "observed_at": None,
        "ready_file": str(deployment_ready_file(deployment_id)) if deployment_id else "",
        "detail": "no daemon identity document",
    }
    identity: dict[str, Any] = {}
    pid: Optional[int] = None
    for path in _identity_paths(deployment_id):
        data = read_identity(path)
        candidate = _pid_from_identity(data)
        if not candidate or candidate == os.getpid():
            continue
        if not _pid_matches_daemon(candidate, deployment_id):
            continue
        identity = data
        pid = candidate
        break
    if pid is None:
        return result

    result["pid"] = pid
    result["liveness_ok"] = True
    reported_version = identity.get("version")
    reported_executable = identity.get("executable")
    reported_impl = identity.get("impl")
    result["version"] = reported_version
    result["executable"] = reported_executable
    result["impl"] = reported_impl
    version_ok = isinstance(reported_version, str) and bool(reported_version)
    if (
        version_ok
        and reported_version == installed_version
        and reported_executable == sys.executable
    ):
        result["identity_ok"] = True
        result["detail"] = f"pid {pid} python {reported_version}"
    elif not version_ok:
        result["detail"] = f"pid {pid} is foreign/legacy (no version in pid/lock file)"
    elif reported_impl == "npm" or (
        reported_executable and reported_executable != sys.executable
    ):
        result["detail"] = (
            f"pid {pid} is foreign impl={reported_impl!r} version={reported_version!r}"
        )
    else:
        result["detail"] = (
            f"pid {pid} reports version {reported_version!r}, "
            f"this process is {installed_version!r}"
        )

    if deployment_id:
        try:
            ready = json.loads(deployment_ready_file(deployment_id).read_text())
        except (OSError, json.JSONDecodeError):
            ready = {}
        if not isinstance(ready, dict):
            ready = {}
        observed = ready.get("observed_at")
        result["observed_at"] = observed
        heartbeat_ok = (
            ready.get("deployment_id") == deployment_id
            and isinstance(observed, (int, float))
            and (time.time() - float(observed)) < HEARTBEAT_STALE_SECONDS
        )
        result["heartbeat_ok"] = heartbeat_ok
        if heartbeat_ok:
            result["ok"] = True
            result["detail"] = f"{result['detail']}, heartbeat fresh"
        else:
            result["detail"] = (
                f"{result['detail']}, ready file missing or stale "
                f"({result['ready_file']})"
            )
    elif result["liveness_ok"]:
        result["ok"] = True
    return result


def is_current_daemon(deployment_id: str = "") -> bool:
    """True when a capable daemon (fresh ready-file heartbeat) owns this deployment."""
    return bool(probe_daemon(deployment_id)["heartbeat_ok"] if deployment_id else probe_daemon(deployment_id)["ok"])


def daemon_startable(secret_key: str) -> bool:
    """True when this process can spawn a replacement daemon."""
    if not secret_key or not str(secret_key).strip():
        return False
    if not sys.executable:
        return False
    return True


def ensure_current_daemon(
    secret_key: str,
    deployment_id: str,
    workspace: Optional[str] = None,
    wait: bool = True,
    force: bool = False,
) -> bool:
    """Keep a capable daemon. Never stop a live owner until spawn is startable."""
    _ = force  # capability (heartbeat), not caller force, decides replacement
    probe = probe_daemon(deployment_id)
    if probe.get("heartbeat_ok"):
        return True
    if not daemon_startable(secret_key):
        return False
    stop_daemon(deployment_id)
    return spawn_detached_daemon(secret_key, deployment_id, workspace, wait=wait)


def _neo_mcp_argv(deployment_id: str, workspace: Optional[str]) -> list[str]:
    """Argv to launch the daemon from this interpreter (VS Code process.execPath)."""
    argv = [sys.executable, "-m", "neo_mcp", "daemon", "--deployment-id", deployment_id]
    if workspace:
        argv.append(workspace)
    return argv


# ---------------------------------------------------------------------------
# Detached spawn (auto-spawn fallback path) — NO stdout writes (MCP-stdio safe)
# ---------------------------------------------------------------------------

def spawn_detached_daemon(
    secret_key: str,
    deployment_id: str,
    workspace: Optional[str] = None,
    wait: bool = False,
) -> bool:
    """Spawn ``neo-mcp daemon`` fully detached from the caller's session.

    ``start_new_session=True`` puts the child in its own session + process
    group with no controlling terminal, so a Ctrl-Z / SIGHUP on the parent
    editor cannot suspend or kill it. All stdio is detached to DEVNULL.

    Returns True if the process was launched (or, when ``wait``, became live).
    Never writes to stdout — safe to call from the MCP stdio server.
    """
    env = os.environ.copy()
    env["NEO_SECRET_KEY"] = secret_key
    try:
        subprocess.Popen(
            _neo_mcp_argv(deployment_id, workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001 — caller logs; must not raise into stdio loop
        return False

    if not wait:
        return True
    for _ in range(20):  # up to 10s
        time.sleep(0.5)
        if is_current_daemon(deployment_id):
            return True
    return False


# ---------------------------------------------------------------------------
# systemd user service (preferred persistent path)
# ---------------------------------------------------------------------------

def systemd_available() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def _systemctl_user(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, timeout=20, check=check,
    )


def _unit_text(deployment_id: str) -> str:
    exec_argv = _neo_mcp_argv(deployment_id, workspace=None)
    exec_start = " ".join(exec_argv)
    return (
        "[Unit]\n"
        "Description=Neo MCP sandbox daemon\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile={_ENV_FILE}\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        # A clean SIGTERM stop must not be treated as failure.
        "SuccessExitStatus=0 143\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_service(secret_key: str, deployment_id: str, workspace: Optional[str] = None) -> int:
    """Install + enable + start a systemd **user** unit for the daemon.

    Secret is written to a 0600 EnvironmentFile, never inlined into the unit.
    Returns a process exit code (0 = success).
    """
    if not systemd_available():
        print("systemd not available on this host.", file=sys.stderr)
        print("Fallback — run the detached daemon manually:", file=sys.stderr)
        print(f"  setsid {' '.join(_neo_mcp_argv(deployment_id, workspace))} "
              ">/dev/null 2>&1 < /dev/null &", file=sys.stderr)
        return 1

    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    # Secret in a 0600 EnvironmentFile (created before writing so the secret
    # never briefly lands in a world-readable file).
    env_lines = f"NEO_SECRET_KEY={secret_key}\n"
    if workspace:
        env_lines += f"NEO_WORKSPACE_DIR={workspace}\n"
    fd = os.open(str(_ENV_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, env_lines.encode())
    finally:
        os.close(fd)
    os.chmod(_ENV_FILE, 0o600)

    _UNIT_PATH.write_text(_unit_text(deployment_id))

    # Best-effort: linger lets the unit run without an active login session
    # (essential on a headless sandbox). Needs privilege; ignore failure.
    user = os.environ.get("USER") or ""
    if user and shutil.which("loginctl"):
        subprocess.run(["loginctl", "enable-linger", user],
                       capture_output=True, text=True, timeout=20)

    _systemctl_user("daemon-reload")
    res = _systemctl_user("enable", "--now", _UNIT_NAME)
    if res.returncode != 0:
        print(f"Failed to enable/start unit:\n{res.stderr}", file=sys.stderr)
        return res.returncode

    print(f"Installed and started systemd user service: {_UNIT_NAME}")
    print(f"  unit:    {_UNIT_PATH}")
    print(f"  env:     {_ENV_FILE} (0600)")
    print(f"  status:  systemctl --user status {_UNIT_NAME}")
    print(f"  logs:    journalctl --user -u {_UNIT_NAME} -f  (or neo-mcp logs)")
    return 0


def service_installed() -> bool:
    return _UNIT_PATH.exists()


def uninstall_service() -> bool:
    """Stop + disable + remove the systemd user unit. Returns True if one existed."""
    existed = _UNIT_PATH.exists()
    if systemd_available():
        _systemctl_user("disable", "--now", _UNIT_NAME)
    try:
        _UNIT_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    if systemd_available():
        _systemctl_user("daemon-reload")
    return existed


# ---------------------------------------------------------------------------
# stop / teardown
# ---------------------------------------------------------------------------

def stop_daemon(deployment_id: str = "", timeout: float = 8.0) -> int:
    """Stop the running daemon (service if installed, else direct signal).

    Returns the number of processes terminated.
    """
    # If managed by systemd, stop via the unit so it isn't auto-restarted.
    if _UNIT_PATH.exists() and systemd_available():
        _systemctl_user("stop", _UNIT_NAME)

    pids = running_daemon_pids(deployment_id)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # Wait for graceful exit, then SIGKILL stragglers.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not running_daemon_pids(deployment_id):
            break
        time.sleep(0.25)
    for pid in running_daemon_pids(deployment_id):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    _clear_runtime_state(deployment_id)
    return len(pids)


def restart(secret_key: str, deployment_id: str, workspace: Optional[str] = None) -> int:
    """Stop then start the daemon (via the service if installed, else detached spawn)."""
    stop_daemon(deployment_id)
    if service_installed() and systemd_available():
        return _systemctl_user("start", _UNIT_NAME).returncode
    if not secret_key:
        print("NEO_SECRET_KEY required to start the daemon.", file=sys.stderr)
        return 1
    return 0 if spawn_detached_daemon(secret_key, deployment_id, workspace, wait=True) else 1


def _clear_runtime_state(deployment_id: str) -> None:
    # Lock + PID files only — NEVER the standalone UUID. In machine-persisted
    # mode the deployment ID is a random uuid4 stored in STANDALONE_UUID_FILE;
    # deleting it would silently re-assign a new ID on the next start and break
    # the backend binding. Identity is removed only by `uninstall --purge`.
    for path in (LOCK_FILE, PID_FILE,
                 _deployment_pid_file(deployment_id) if deployment_id else None):
        if path:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# MCP config stripping (reverse of setup._CONFIGURATORS)
# ---------------------------------------------------------------------------

def _strip_json_key(path: Path, *keys: str) -> bool:
    """Remove nested ``data[k1][k2]…[-1] == 'neo'`` if present. Returns True if changed."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    node = data
    for k in keys[:-1]:
        if not isinstance(node, dict) or k not in node:
            return False
        node = node[k]
    leaf = keys[-1]
    if isinstance(node, dict) and leaf in node:
        del node[leaf]
        try:
            path.write_text(json.dumps(data, indent=2) + "\n")
            return True
        except OSError:
            return False
    return False


def strip_mcp_configs() -> list[str]:
    """Remove the ``neo`` server entry this package added to each editor config."""
    removed: list[str] = []
    home = Path.home()

    # Claude Code — added via `claude mcp add`; remove via CLI across scopes.
    if shutil.which("claude"):
        for scope in ("user", "local", "project"):
            r = subprocess.run(["claude", "mcp", "remove", "--scope", scope, "neo"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                removed.append(f"claude ({scope})")

    targets = [
        (home / ".cursor" / "mcp.json", ("mcpServers", "neo")),
        (home / ".codeium" / "windsurf" / "mcp_config.json", ("mcpServers", "neo")),
        (home / ".config" / "zed" / "settings.json", ("context_servers", "neo")),
        (Path.cwd() / ".vscode" / "mcp.json", ("servers", "neo")),
        (home / ".codex" / "config.json", ("mcpServers", "neo")),
        # Claude Desktop fallback locations
        (home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json", ("mcpServers", "neo")),
        (home / ".config" / "Claude" / "claude_desktop_config.json", ("mcpServers", "neo")),
    ]
    for path, keys in targets:
        if _strip_json_key(path, *keys):
            removed.append(str(path))

    # Continue uses a LIST of servers keyed by "name".
    cont = home / ".continue" / "config.json"
    if cont.exists():
        try:
            data = json.loads(cont.read_text())
            servers = data.get("mcpServers")
            if isinstance(servers, list):
                kept = [s for s in servers if s.get("name") != "neo"]
                if len(kept) != len(servers):
                    data["mcpServers"] = kept
                    cont.write_text(json.dumps(data, indent=2) + "\n")
                    removed.append(str(cont))
        except (OSError, ValueError):
            pass
    return removed


# ---------------------------------------------------------------------------
# uninstall (full teardown)
# ---------------------------------------------------------------------------

def uninstall(deployment_id: str = "", purge: bool = False) -> int:
    """Stop the daemon and remove everything ``setup`` installed.

    Default keeps credentials/identity (``~/.neo`` auth + UUID) so re-install is
    seamless. ``purge=True`` wipes all of ``~/.neo``.
    """
    print("Tearing down neo-mcp…")

    had_service = uninstall_service()
    if had_service:
        print("  • systemd user service: stopped, disabled, removed")

    killed = stop_daemon(deployment_id)
    print(f"  • daemon processes terminated: {killed}")

    removed_cfgs = strip_mcp_configs()
    if removed_cfgs:
        print(f"  • removed 'neo' MCP entry from: {', '.join(removed_cfgs)}")
    else:
        print("  • no editor MCP entries found to remove")

    # Installed skill
    try:
        if _SKILL_FILE.exists():
            _SKILL_FILE.unlink()
            print(f"  • removed skill: {_SKILL_FILE}")
    except OSError:
        pass

    # Runtime artifacts (logs, jobs) — safe to drop
    for path in (THREAD_WORKSPACES_FILE, JOBS_FILE, _ENV_FILE):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        if JOBS_LOG_DIR.exists():
            shutil.rmtree(JOBS_LOG_DIR, ignore_errors=True)
    except OSError:
        pass

    if purge:
        try:
            shutil.rmtree(NEO_DIR, ignore_errors=True)
            print(f"  • purged all state: {NEO_DIR}")
        except OSError:
            pass
    else:
        print(f"  • kept credentials/identity in {NEO_DIR} (use --purge to wipe)")

    print("Done. Finish with: pip uninstall neo-mcp")
    return 0
