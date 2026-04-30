"""Gather execution-environment metadata and format it as XML for Neo backend.

Mirrors the SystemInfoGatherer in the VS Code extension so the backend receives
the same structured context regardless of client type.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_DURATION_S = 300  # 5-minute cache for system-level probes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def _normalize_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("x86_64", "amd64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("arm"):
        return "arm"
    return m


def _normalize_os(system: str) -> str:
    s = system.lower()
    if s == "darwin":
        return "darwin"
    if s == "windows":
        return "windows"
    return "linux"


def _bytes_to_gb(b: int) -> float:
    return round(b / (1024 ** 3) * 10) / 10


async def _exec(*args: str, cwd: Optional[str] = None, timeout: float = 5.0) -> Optional[str]:
    """Run a command with explicit args (no shell) and return stripped stdout, or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-capability probes
# ---------------------------------------------------------------------------

async def _get_shell_info() -> dict:
    if sys.platform == "win32":
        default = os.environ.get("COMSPEC", "C:\\Windows\\System32\\cmd.exe")
        candidates = [
            "C:\\Windows\\System32\\cmd.exe",
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        ]
        available = [p for p in candidates if os.path.isfile(p)]
        if not available:
            available = [default]
    else:
        default = os.environ.get("SHELL", "/bin/sh")
        candidates = ["/bin/bash", "/bin/zsh", "/bin/sh", "/bin/fish", "/bin/dash"]
        available = [p for p in candidates if os.path.isfile(p) and os.access(p, os.X_OK)]
        if not available:
            available = ["/bin/sh"]
    return {"default": default, "available": available}


async def _get_python_info() -> dict:
    cmds = ("python3", "python") if sys.platform != "win32" else ("python", "python3")
    for cmd in cmds:
        out = await _exec(cmd, "--version")
        if out:
            m = re.search(r"Python ([\d.]+)", out)
            if m:
                path_out = await _exec(cmd, "-c", "import sys; print(sys.executable)")
                return {
                    "available": True,
                    "version": m.group(1),
                    "path": path_out or cmd,
                }
    return {"available": False}


async def _get_git_info() -> dict:
    out = await _exec("git", "--version")
    if out:
        m = re.search(r"git version ([\d.]+)", out)
        return {"available": True, "version": m.group(1) if m else "unknown"}
    return {"available": False}


async def _get_docker_info() -> dict:
    out = await _exec("docker", "--version")
    if out:
        m = re.search(r"Docker version ([\d.]+)", out)
        return {"available": True, "version": m.group(1) if m else "unknown"}
    return {"available": False}


async def _get_gpu_info() -> dict:
    out = await _exec(
        "nvidia-smi",
        "--query-gpu=name,memory.total",
        "--format=csv,noheader",
    )
    if out:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if lines:
            parts = lines[0].split(",", 1)
            name = parts[0].strip()
            # nvidia-smi sometimes writes its error to stdout instead of stderr
            if "has failed" in name.lower() or "error" in name.lower():
                return {"available": False}
            vram: Optional[int] = None
            if len(parts) > 1:
                m = re.search(r"(\d+)", parts[1])
                if m:
                    vram = round(int(m.group(1)) / 1024)
            result: dict = {"available": True, "count": len(lines), "name": name}
            if vram is not None:
                result["vram_gb"] = vram
            return result
    return {"available": False}


def _get_mcp_version() -> str:
    try:
        from importlib.metadata import version
        return version("neo-mcp")
    except Exception:
        return "unknown"


def _get_ram() -> tuple[float, float]:
    """Return (total_gb, available_gb). Falls back to zeros on non-Linux."""
    try:
        total = _bytes_to_gb(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))  # type: ignore[attr-defined]
        avail = _bytes_to_gb(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES"))  # type: ignore[attr-defined]
        return total, avail
    except (AttributeError, ValueError, OSError):
        pass
    # Fallback: read /proc/meminfo on Linux without sysconf
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                meminfo[key.strip()] = int(val.split()[0]) * 1024  # kB → bytes
        total = _bytes_to_gb(meminfo.get("MemTotal", 0))
        avail = _bytes_to_gb(meminfo.get("MemAvailable", 0))
        return total, avail
    except Exception:
        return 0.0, 0.0


# ---------------------------------------------------------------------------
# Gatherer
# ---------------------------------------------------------------------------

class SystemInfoGatherer:
    """Async, cached gatherer for execution-environment metadata."""

    def __init__(self) -> None:
        self._cache: Optional[dict] = None
        self._cache_ts: float = 0.0

    async def gather(self) -> dict:
        now = time.monotonic()
        if self._cache and (now - self._cache_ts) < _CACHE_DURATION_S:
            return self._cache

        total_ram, avail_ram = _get_ram()

        shell, python, git, docker, gpu = await asyncio.gather(
            _get_shell_info(),
            _get_python_info(),
            _get_git_info(),
            _get_docker_info(),
            _get_gpu_info(),
        )

        info: dict = {
            "application": "vscode",
            "version": _get_mcp_version(),
            "platform": "local",
            "ide": "neo-mcp",
            "system": {
                "os": _normalize_os(platform.system()),
                "arch": _normalize_arch(platform.machine()),
                "cpu_cores": os.cpu_count() or 1,
                "ram_total_gb": total_ram,
                "ram_available_gb": avail_ram,
                "shell": shell,
            },
            "capabilities": {
                "python": python,
                "git": git,
                "docker": docker,
                "gpu": gpu,
            },
        }
        self._cache = info
        self._cache_ts = now
        return info

    def format_xml(self, env: dict) -> str:
        """Return inner XML content (caller wraps in <exec_environment>)."""
        s = env["system"]
        c = env["capabilities"]
        lines: list[str] = []

        lines.append(f'  <application>{_escape_xml(env["application"])}</application>')
        lines.append(f'  <version>{_escape_xml(env["version"])}</version>')
        lines.append(f'  <platform>{_escape_xml(env["platform"])}</platform>')
        lines.append(f'  <ide>{_escape_xml(env["ide"])}</ide>')
        lines.append("")
        lines.append("  <system>")
        lines.append(f'    <os>{_escape_xml(s["os"])}</os>')
        lines.append(f'    <arch>{_escape_xml(s["arch"])}</arch>')
        lines.append(f'    <cpu_cores>{s["cpu_cores"]}</cpu_cores>')
        lines.append(f'    <ram_total_gb>{s["ram_total_gb"]}</ram_total_gb>')
        lines.append(f'    <ram_available_gb>{s["ram_available_gb"]}</ram_available_gb>')
        lines.append("  </system>")
        lines.append("")
        lines.append("  <shell>")
        lines.append(f'    <default>{_escape_xml(s["shell"]["default"])}</default>')
        lines.append(f'    <available>{json.dumps(s["shell"]["available"])}</available>')
        lines.append("  </shell>")
        lines.append("")
        lines.append("  <capabilities>")

        py = c["python"]
        lines.append("    <python>")
        lines.append(f'      <available>{str(py["available"]).lower()}</available>')
        if py.get("version"):
            lines.append(f'      <version>{_escape_xml(py["version"])}</version>')
        if py.get("path"):
            lines.append(f'      <path>{_escape_xml(py["path"])}</path>')
        lines.append("    </python>")

        g = c["git"]
        lines.append("    <git>")
        lines.append(f'      <available>{str(g["available"]).lower()}</available>')
        if g.get("version"):
            lines.append(f'      <version>{_escape_xml(g["version"])}</version>')
        lines.append("    </git>")

        dk = c["docker"]
        lines.append("    <docker>")
        lines.append(f'      <available>{str(dk["available"]).lower()}</available>')
        if dk.get("version"):
            lines.append(f'      <version>{_escape_xml(dk["version"])}</version>')
        lines.append("    </docker>")

        gpu = c["gpu"]
        lines.append("    <gpu>")
        lines.append(f'      <available>{str(gpu["available"]).lower()}</available>')
        if gpu.get("count"):
            lines.append(f'      <count>{gpu["count"]}</count>')
        if gpu.get("name"):
            lines.append(f'      <name>{_escape_xml(gpu["name"])}</name>')
        if gpu.get("vram_gb") is not None:
            lines.append(f'      <vram_gb>{gpu["vram_gb"]}</vram_gb>')
        lines.append("    </gpu>")

        lines.append("  </capabilities>")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git branch detection
# ---------------------------------------------------------------------------

async def get_git_branch(workspace: str) -> Optional[str]:
    """Return the current git branch in *workspace*, or None."""
    branch = await _exec(
        "git", "rev-parse", "--abbrev-ref", "HEAD",
        cwd=workspace,
        timeout=3.0,
    )
    return branch if branch and branch != "HEAD" else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Module-level singleton shared across all tool calls within one server process
_gatherer = SystemInfoGatherer()


async def build_context_prefix(workspace: str) -> str:
    """Return the metadata XML block to prepend to a task message."""
    exec_env_block = ""
    try:
        env = await _gatherer.gather()
        inner_xml = _gatherer.format_xml(env)
        exec_env_block = f"<exec_environment>\n{inner_xml}\n</exec_environment>"
    except Exception as exc:
        logger.warning("system_info: could not gather exec_environment: %s", exc)

    branch = await get_git_branch(workspace)

    parts: list[str] = []
    if exec_env_block:
        parts.append(exec_env_block)
    parts.append(f"<current_work_dir>{_escape_xml(workspace)}</current_work_dir>")
    if branch:
        parts.append(f"<git_branch>{_escape_xml(branch)}</git_branch>")

    return "\n".join(parts)
