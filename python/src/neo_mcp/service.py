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
from pathlib import Path
from typing import Optional

from .paths import (
    DAEMON_DIR,
    JOBS_FILE,
    JOBS_LOG_DIR,
    LOCK_FILE,
    NEO_DIR,
    PID_FILE,
    THREAD_WORKSPACES_FILE,
)

_ENV_FILE = DAEMON_DIR / "neo-mcp.env"
_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
_UNIT_NAME = "neo-mcp.service"
_UNIT_PATH = _SYSTEMD_USER_DIR / _UNIT_NAME
_SKILL_FILE = Path.home() / ".claude" / "skills" / "neo.md"


# ---------------------------------------------------------------------------
# PID / liveness helpers
# ---------------------------------------------------------------------------

def _deployment_pid_file(deployment_id: str) -> Path:
    return DAEMON_DIR / f"daemon_{deployment_id.replace('-', '')[:8]}.pid"


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


def running_daemon_pids(deployment_id: str = "") -> list[int]:
    """Collect candidate daemon PIDs from the lock + pid files (deduped, alive)."""
    pids: set[int] = set()
    for path in (PID_FILE, _deployment_pid_file(deployment_id) if deployment_id else None):
        if path and path.exists():
            try:
                pids.add(int(path.read_text().strip()))
            except (OSError, ValueError):
                pass
    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text())
            if isinstance(data, dict) and data.get("pid"):
                pids.add(int(data["pid"]))
        except (OSError, ValueError, TypeError):
            pass
    return [p for p in pids if _pid_alive(p)]


def _neo_mcp_argv(deployment_id: str, workspace: Optional[str]) -> list[str]:
    """Argv to launch the daemon: prefer the console script, fall back to ``-m``."""
    neo_bin = shutil.which("neo-mcp")
    base = [neo_bin] if neo_bin else [sys.executable, "-m", "neo_mcp"]
    argv = base + ["daemon", "--deployment-id", deployment_id]
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
        if running_daemon_pids(deployment_id):
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
