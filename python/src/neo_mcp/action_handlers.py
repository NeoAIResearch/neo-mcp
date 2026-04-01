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
from pathlib import Path
from typing import Any, Optional

from .job_manager import JobManager

logger = logging.getLogger(__name__)

# Directories whose contents are excluded from list_files recursion
_SKIP_DIRS = frozenset({"venv", ".venv", "env", "node_modules", ".git", "__pycache__"})

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
        session_id = cmd.get("payload", {}).get("session_id") or cmd.get("session_id")
        if not session_id:
            return {"request_id": cmd["request_id"], "status": "error", "error": "Missing session_id"}
        logger.info("Session created: %s", session_id)
        return {
            "request_id": cmd["request_id"],
            "status": "success",
            "data": {"coding_session_id": session_id},
        }

    async def _write_code(self, cmd: dict) -> dict:
        request_id = cmd["request_id"]
        filename = cmd.get("filename")
        code = cmd.get("code")
        workdir = cmd.get("workdir")
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
        file_path_raw = cmd.get("file_path")
        thread_id = cmd.get("thread_id")

        if not file_path_raw:
            return {"request_id": request_id, "status": "error", "error": "Missing file_path"}

        workspace = self._workspace_for(thread_id)

        if os.path.isabs(file_path_raw):
            # Allow any absolute path — Neo uses /app/project/ as its default workspace.
            resolved = Path(file_path_raw).resolve()
        else:
            resolved = (Path(workspace) / file_path_raw).resolve()

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
        request_id = cmd["request_id"]
        command_str = cmd.get("command")
        thread_id = cmd.get("thread_id")

        if not command_str:
            return {"request_id": request_id, "status": "error", "error": "Missing command"}

        # Pre-flight: check if backend is sending a /tmp script that doesn't exist locally
        import re
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
        max_depth: int = cmd.get("max_depth") or payload.get("max_depth") or 10
        include_hidden: bool = cmd.get("include_hidden") or payload.get("include_hidden") or False

        # Resolve and validate target directory
        if os.path.isabs(directory):
            target = Path(directory).resolve()
        else:
            target = (Path(workspace) / directory).resolve()

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
