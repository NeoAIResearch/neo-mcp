"""Command execution handlers — Python port of DaemonActionHandlers.ts.

Handles 7 action types dispatched by BackendPoller:
  create_session  — acknowledge session with coding_session_id
  write_code      — write file to workspace (path-safety validated)
  get_file        — read file from workspace or /tmp
  run_subprocess  — start sh -c <cmd> via JobManager, return job_id
  get_job_status  — return stdout/stderr/exit_code snapshot
  terminate_job   — SIGTERM → SIGKILL the job
  list_files      — walk directory, return path|type|size lines

Security: all file paths are validated to be within the thread's workspace
or /tmp — no traversal outside those boundaries is allowed.
"""

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from .job_manager import JobManager

logger = logging.getLogger(__name__)

# Directories whose contents are excluded from list_files recursion.
# Mirrors npm executor.ts SKIP_DIRS — keep both in sync.
_SKIP_DIRS = frozenset({
    "venv", ".venv", "env",          # Python virtualenvs
    "node_modules",                   # JS deps
    ".git",                           # version control
    "__pycache__", ".tox",            # Python build artefacts
    "dist", "build",                  # build output
})

# Temp directories that are always permitted for file operations
_TMP_DIRS = [Path("/tmp"), Path("/private/tmp")]
try:
    import tempfile
    _TMP_DIRS.append(Path(tempfile.gettempdir()).resolve())
except Exception:
    pass


class ActionHandlers:
    def __init__(
        self,
        job_manager: JobManager,
        default_workspace: str,
        thread_workspaces: dict[str, str],
    ) -> None:
        self._job_manager = job_manager
        self._default_workspace = default_workspace
        self._thread_workspaces = thread_workspaces  # shared mutable dict

    def update_workspace(self, workspace: str) -> None:
        self._default_workspace = workspace

    # ------------------------------------------------------------------
    # Public dispatch entry point
    # ------------------------------------------------------------------

    async def handle_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """Route command to the appropriate handler and return a response dict."""
        action = command.get("action", "")
        request_id = command.get("request_id", "")

        handlers = {
            "create_session": self._create_session,
            "write_code": self._write_code,
            "get_file": self._get_file,
            "run_subprocess": self._run_subprocess,
            "get_job_status": self._get_job_status,
            "terminate_job": self._terminate_job,
            "list_files": self._list_files,
        }

        handler = handlers.get(action)
        if handler is None:
            return {"request_id": request_id, "status": "error", "error": f"Unknown action: {action}"}

        try:
            return await handler(command)
        except Exception as exc:  # noqa: BLE001
            logger.error("Handler %s failed: %s", action, exc, exc_info=True)
            return {"request_id": request_id, "status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _create_session(self, cmd: dict) -> dict:
        session_id = (
            cmd.get("payload", {}).get("session_id")
            or cmd.get("session_id")
            or str(uuid.uuid4())  # generate one if backend omits it (matches npm daemon)
        )
        logger.info("Session created: %s", session_id)
        return {
            "request_id": cmd["request_id"],
            "status": "success",
            "data": {"coding_session_id": session_id},
        }

    async def _write_code(self, cmd: dict) -> dict:
        request_id = cmd["request_id"]
        payload = cmd.get("payload") or {}
        # Check top-level first, fall back to payload (mirrors npm fieldString()).
        filename = cmd.get("filename") or payload.get("filename")
        # code may be empty string ("") which is valid — only fall back if truly absent (None).
        _code_top = cmd.get("code")
        code = _code_top if _code_top is not None else payload.get("code")
        workdir = cmd.get("workdir") or payload.get("workdir")
        thread_id = cmd.get("thread_id")

        if not filename or code is None:
            return {"request_id": request_id, "status": "error", "error": "Missing filename or code"}

        workspace = self._workspace_for(thread_id)
        ws_resolved = Path(workspace).resolve()

        if os.path.isabs(filename):
            candidate = Path(filename).resolve()
            if self._is_allowed_path(candidate, ws_resolved):
                file_path = candidate
            else:
                # Backend sent its own container path (e.g. /app/project/src/main.py).
                # Remap to the user's local workspace preserving relative structure.
                file_path = self._remap_to_workspace(candidate, ws_resolved, workdir)
                logger.info("Remapped absolute path %s → %s", filename, file_path)
        else:
            if workdir and os.path.isabs(workdir):
                # Backend supplied an absolute workdir (e.g. /app/project/test_2/demo).
                # Remap it to the local workspace to preserve subdirectory structure.
                # e.g. /app/project/test_2/demo → <workspace>/test_2/demo
                base = self._remap_to_workspace(Path(workdir).resolve(), ws_resolved)
            else:
                base = ws_resolved / workdir if workdir else ws_resolved
            candidate = (base / filename).resolve()
            if not self._is_allowed_path(candidate, ws_resolved):
                logger.warning("Path traversal blocked: %s", filename)
                return {"request_id": request_id, "status": "error", "error": "Path traversal detected"}
            file_path = candidate

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(code, encoding="utf-8")
        logger.info("File written: %s", file_path)
        return {
            "request_id": request_id,
            "status": "success",
            "data": {"file_path": str(file_path), "workdir": workdir or ""},
        }

    async def _get_file(self, cmd: dict) -> dict:
        request_id = cmd["request_id"]
        payload = cmd.get("payload") or {}
        file_path_raw = cmd.get("file_path") or payload.get("file_path")
        thread_id = cmd.get("thread_id")

        if not file_path_raw:
            return {"request_id": request_id, "status": "error", "error": "Missing file_path"}

        workspace = self._workspace_for(thread_id)

        ws_resolved = Path(workspace).resolve()
        if os.path.isabs(file_path_raw):
            candidate = Path(file_path_raw).resolve()
            if self._is_allowed_path(candidate, ws_resolved):
                resolved = candidate
            else:
                # Backend container path (e.g. /app/project/src/file.py) — remap to local workspace
                resolved = self._remap_to_workspace(candidate, ws_resolved)
        else:
            resolved = (ws_resolved / file_path_raw).resolve()
            if not self._is_allowed_path(resolved, ws_resolved):
                logger.warning("Path traversal blocked in get_file: %s", file_path_raw)
                return {"request_id": request_id, "status": "error", "error": "Path traversal detected"}

        if not resolved.exists():
            return {"request_id": request_id, "status": "error", "error": "File not found"}

        content = resolved.read_text(encoding="utf-8", errors="replace")
        logger.info("File read: %s (%d chars)", resolved, len(content))
        return {
            "request_id": request_id,
            "status": "success",
            "data": {"file_content": content, "file_path": str(resolved)},
        }

    async def _run_subprocess(self, cmd: dict) -> dict:
        import asyncio
        import re

        request_id = cmd["request_id"]
        payload = cmd.get("payload") or {}
        command_str = cmd.get("command") or payload.get("command")
        thread_id = cmd.get("thread_id")

        if not command_str:
            return {"request_id": request_id, "status": "error", "error": "Missing command"}

        # Normalise the detach flag — backend may send bool or string.
        raw_detach = cmd.get("detach", True)
        if isinstance(raw_detach, str):
            detach = raw_detach.lower() not in ("false", "0", "no")
        else:
            detach = bool(raw_detach)

        # Pre-flight: check if backend is sending a /tmp script that doesn't exist locally
        m = re.search(r"(?:bash|sh)\s+(/tmp/bash_exec_[a-f0-9]+\.sh)", command_str)
        if m:
            script_path = Path(m.group(1))
            if not script_path.exists():
                logger.error("Script not found locally: %s", script_path)
                return {
                    "request_id": request_id,
                    "status": "error",
                    "error": (
                        f"Script not found: {script_path}. "
                        "Backend must send 'write_code' before 'run_subprocess'."
                    ),
                }

        workspace = self._workspace_for(thread_id)
        ws_path = Path(workspace).resolve()

        # Remap container paths in the command string so Neo backend commands like
        # `ls /app/project/foo` work on the host filesystem — mirrors npm remapCommandPaths().
        remapped_cmd = self._remap_command_paths(command_str, ws_path)
        if remapped_cmd != command_str:
            logger.info("run_subprocess: remapped paths: %s → %s", command_str[:80], remapped_cmd[:80])
        command_str = remapped_cmd

        # Ensure workspace exists before spawning — mirrors npm mkdirSync(safeCwd).
        ws_path.mkdir(parents=True, exist_ok=True)

        if not detach:
            # Blocking (synchronous) mode — mirrors npm executor.ts hRunSubprocess detach=false.
            # Run command to completion and return stdout/stderr immediately in the response.
            proc = await asyncio.create_subprocess_shell(
                command_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            exit_code = proc.returncode or 0
            logger.info(
                "Subprocess (blocking) done: exit=%d cmd=%r", exit_code, command_str[:80]
            )
            return {
                "request_id": request_id,
                "status": "completed" if exit_code == 0 else "error",
                "data": {
                    "detached": False,
                    "completed": True,
                    "exit_code": exit_code,
                    "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                    "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                },
                **({"error": f"Command failed with exit code {exit_code}"} if exit_code != 0 else {}),
            }

        job_id = await self._job_manager.create_job(command_str, workspace, thread_id or "unknown")
        logger.info("Subprocess started: job_id=%s cmd=%r", job_id, command_str[:80])
        return {
            "request_id": request_id,
            "status": "success",
            "data": {"job_id": job_id, "detached": True, "message": "Job started in background"},
        }

    async def _get_job_status(self, cmd: dict) -> dict:
        request_id = cmd["request_id"]
        job_id = cmd.get("job_id")
        if not job_id:
            return {"request_id": request_id, "status": "error", "error": "Missing job_id"}

        logs = self._job_manager.get_job_logs(job_id)
        if logs is None:
            return {"request_id": request_id, "status": "error", "error": "Job not found"}

        is_completed = logs["exit_code"] is not None
        return {
            "request_id": request_id,
            "status": "completed" if is_completed else "pending",
            "data": {
                "job_id": job_id,
                "stdout": logs["stdout"],
                "stderr": logs["stderr"],
                "exit_code": logs["exit_code"],
                "completed": is_completed,
            },
        }

    async def _terminate_job(self, cmd: dict) -> dict:
        request_id = cmd["request_id"]
        job_id = cmd.get("job_id")
        if not job_id:
            return {"request_id": request_id, "status": "error", "error": "Missing job_id"}

        ok = self._job_manager.terminate_job(job_id)
        if not ok:
            return {"request_id": request_id, "status": "error", "error": "Job not found or already completed"}

        logger.info("Job terminated: %s", job_id)
        return {"request_id": request_id, "status": "success", "data": {"job_id": job_id, "terminated": True}}

    async def _list_files(self, cmd: dict) -> dict:
        request_id = cmd["request_id"]
        thread_id = cmd.get("thread_id")
        workspace = self._workspace_for(thread_id)

        payload = cmd.get("payload") or {}
        directory = cmd.get("directory") or payload.get("directory") or workspace
        # Use explicit None check so max_depth=0 ("unlimited" on backend) is not lost to or-chain.
        _raw_depth = cmd.get("max_depth") if cmd.get("max_depth") is not None else payload.get("max_depth")
        max_depth: int = int(_raw_depth) if _raw_depth is not None else 10
        include_hidden: bool = bool(cmd.get("include_hidden") or payload.get("include_hidden") or False)

        # Resolve and validate target directory
        ws_resolved = Path(workspace).resolve()
        if os.path.isabs(directory):
            candidate = Path(directory).resolve()
            if self._is_allowed_path(candidate, ws_resolved):
                target = candidate
            else:
                # Backend container path (e.g. /app/project) — remap to local workspace
                target = self._remap_to_workspace(candidate, ws_resolved)
                logger.info("list_files: remapped %s → %s", directory, target)
        else:
            target = (ws_resolved / directory).resolve()

        if not target.exists():
            return {"request_id": request_id, "status": "error", "error": f"Directory not found: {target}"}
        if not target.is_dir():
            return {"request_id": request_id, "status": "error", "error": f"Not a directory: {target}"}

        lines: list[str] = []

        def walk(path: Path, depth: int) -> None:
            if max_depth > 0 and depth >= max_depth:
                return
            try:
                entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name))
            except PermissionError:
                return
            for entry in entries:
                if not include_hidden and entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    lines.append(f"{entry}|d|0")
                    if entry.name not in _SKIP_DIRS:
                        walk(entry, depth + 1)
                elif entry.is_file():
                    try:
                        lines.append(f"{entry}|f|{entry.stat().st_size}")
                    except OSError:
                        pass

        walk(target, 0)
        logger.info("Listed %d entries in %s", len(lines), target)
        return {
            "request_id": request_id,
            "status": "success",
            "data": {"stdout": "\n".join(lines), "file_count": len(lines), "directory": str(target)},
        }

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _workspace_for(self, thread_id: Optional[str]) -> str:
        if thread_id and thread_id in self._thread_workspaces:
            return self._thread_workspaces[thread_id]
        return self._default_workspace

    def _remap_command_paths(self, command: str, workspace: Path) -> str:
        """Replace known container-root paths in a shell command with local workspace paths.

        The Neo backend constructs shell commands using its own container paths
        (e.g. ``ls /app/project/foo``).  Without remapping those paths don't exist
        on the host and the command fails.  Mirrors npm remapCommandPaths().

        Roots are tried longest-first so /app/project matches before /app.
        Trailing slashes on matched paths are preserved.
        """
        roots = ['/app/project', '/workspace', '/project', '/app']
        result = command
        for root in roots:
            pattern = re.escape(root) + r'(/[^\s\'"`;|&<>(){}\[\]\\]*)?'
            root_path = Path(root)

            def _replace(m: re.Match, _root: Path = root_path, _ws: Path = workspace) -> str:
                path_str = m.group(0)
                trailing = path_str.endswith('/')
                stripped = path_str.rstrip('/')
                path = Path(stripped) if stripped else _root
                remapped = self._remap_to_workspace(path, _ws)
                result_str = str(remapped)
                if trailing and not result_str.endswith('/'):
                    result_str += '/'
                return result_str

            result = re.sub(pattern, _replace, result)
        return result

    def _remap_to_workspace(self, path: Path, workspace: Path, workdir_hint: Optional[str] = None) -> Path:
        """Remap a backend container path (e.g. /app/project/src/main.py) to the local workspace.

        Also deduplicates when workspace is itself a subdirectory matching the first
        segment of the relative path — prevents double-nesting like test_2/test_2/file.py.
        """
        relative: Optional[Path] = None

        if workdir_hint and os.path.isabs(workdir_hint):
            try:
                relative = path.relative_to(Path(workdir_hint).resolve())
            except ValueError:
                pass

        if relative is None:
            for root in [Path("/app/project"), Path("/app"), Path("/workspace"), Path("/project")]:
                try:
                    relative = path.relative_to(root)
                    break
                except ValueError:
                    continue

        if relative is None:
            return workspace / path.name

        # Deduplicate: if workspace ends with the first part of relative,
        # the user's workspace IS that directory — don't nest it again.
        # e.g. workspace=/project/test_2, relative=test_2/file.py → file.py
        parts = relative.parts
        if parts and workspace.parts and workspace.parts[-1] == parts[0]:
            relative = Path(*parts[1:]) if len(parts) > 1 else Path(".")

        if str(relative) == ".":
            return workspace
        return workspace / relative

    def _is_allowed_path(self, resolved: Path, workspace: Path) -> bool:
        ws = workspace.resolve()
        if str(resolved).startswith(str(ws) + os.sep) or resolved == ws:
            return True
        for tmp in _TMP_DIRS:
            t = tmp.resolve()
            if str(resolved).startswith(str(t) + os.sep) or resolved == t:
                return True
        return False
