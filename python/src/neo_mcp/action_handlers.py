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

from .integrations import IntegrationManager
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
        integrations: Optional[IntegrationManager] = None,
    ) -> None:
        self._job_manager = job_manager
        self._default_workspace = default_workspace
        self._thread_workspaces = thread_workspaces  # shared mutable dict
        self._integrations = integrations if integrations is not None else IntegrationManager()
        # Per-thread Neo project slug (e.g. "movie_recommender_system_1703"), captured
        # the first time we see an absolute container path for the thread. Used to
        # rewrite *relative* wrapper references that Neo embeds inside shell scripts
        # (`mkdir -p <slug>/data`) — those aren't caught by _remap_command_paths because
        # there's no syntactic marker. Without this, scripts executed with cwd=<workspace>
        # create <workspace>/<slug>/data instead of <workspace>/data.
        self._thread_wrappers: dict[str, str] = {}

    def update_workspace(self, workspace: str) -> None:
        self._default_workspace = workspace

    # ------------------------------------------------------------------
    # Wrapper-slug tracking — for relative-path rewrites in scripts/commands
    # ------------------------------------------------------------------

    _CONTAINER_ROOTS = (Path("/app/project"), Path("/app"), Path("/workspace"), Path("/project"))

    def _extract_wrapper(self, abs_path: Path) -> Optional[str]:
        """Return Neo's project-name wrapper if abs_path is /<container-root>/<wrapper>/...

        Examples:
            /app/movie_recommender_system_1703/data/x.txt → "movie_recommender_system_1703"
            /app/project/foo/bar.py                       → "foo"
            /tmp/script.sh                                → None
        """
        for root in self._CONTAINER_ROOTS:
            try:
                rel = abs_path.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) >= 2:  # need wrapper + something after
                return parts[0]
            return None
        return None

    def _record_wrapper(self, thread_id: Optional[str], abs_path: Path) -> None:
        if not thread_id or thread_id in self._thread_wrappers:
            return
        slug = self._extract_wrapper(abs_path)
        if slug:
            self._thread_wrappers[thread_id] = slug
            logger.info("Recorded Neo project wrapper for thread %s: %r", thread_id, slug)

    def _strip_wrapper_prefixes(
        self, text: str, wrapper: str, workspace: Optional[Path] = None,
    ) -> str:
        """Rewrite Neo's project-wrapper references in shell text to host paths.

        Neo assumes cwd = /app/<wrapper>/ in its container. On the host the daemon
        runs with cwd = <workspace>, so:

        - Absolute refs `/<container-root>/<wrapper>[/...]` must become `<workspace>[/...]`
          (otherwise scripts walk the host's real /app/ — which on dev machines is
          often polluted with unrelated directories from prior runs).
        - Relative refs like `mkdir -p <wrapper>/data` or `cd <wrapper>` need to
          lose the wrapper so they resolve against <workspace>.

        The absolute-remap step only runs when `workspace` is supplied.
        """
        # Step 1: absolute-container-path remap. Replace /<root>/<wrapper>[/...] with
        # the host workspace. Longest container roots first so /app/project beats /app.
        if workspace is not None:
            ws_str = str(workspace)
            roots_sorted = sorted(
                (str(r) for r in self._CONTAINER_ROOTS),
                key=len,
                reverse=True,
            )
            for root in roots_sorted:
                # Match /root/wrapper as a full path segment. Trailing context must
                # be a path separator, whitespace, quote, closing paren, or end of
                # string — so `/app/my_proj_0001` and `/app/my_proj_0001/foo` both
                # match but `/app/my_proj_0001_backup` does not.
                text = re.sub(
                    rf'{re.escape(root)}/{re.escape(wrapper)}(?=[/\s\'"\)]|$)',
                    ws_str,
                    text,
                )
        # Step 2: strip leading "<wrapper>/" relative references. Lookbehind now
        # also excludes `/` — any `X/<wrapper>/` that survived step 1 is part of
        # a path under an unknown root and should be left alone.
        text = re.sub(rf'(?<![A-Za-z0-9_/]){re.escape(wrapper)}/', '', text)
        # Step 3: bare "<wrapper>" token (no trailing /) → "." for `cd <wrapper>` style.
        text = re.sub(rf'(?<![A-Za-z0-9_/]){re.escape(wrapper)}(?![A-Za-z0-9_])', '.', text)
        return text

    def _apply_wrapper_rewrite(self, text: str, thread_id: Optional[str]) -> str:
        slug = self._thread_wrappers.get(thread_id) if thread_id else None
        if not slug:
            return text
        workspace = Path(self._workspace_for(thread_id)).resolve()
        rewritten = self._strip_wrapper_prefixes(text, slug, workspace)
        if rewritten != text:
            logger.info("Stripped wrapper %r from %d chars of shell text", slug, len(text))
        return rewritten

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

        logger.info("write_code: filename=%r workdir=%r workspace=%s", filename, workdir, ws_resolved)

        # Normalize container-relative filenames to absolute paths so they go through
        # the standard remap logic below. Backend sometimes sends paths without a leading
        # '/' (e.g. "app/project/myproj/model.py") which would otherwise land verbatim
        # under the workspace (e.g. workspace/app/project/myproj/model.py).
        _CONTAINER_REL_PREFIXES = ("app/project/", "app/", "workspace/", "project/")
        if not os.path.isabs(filename) and any(filename.startswith(p) for p in _CONTAINER_REL_PREFIXES):
            logger.info("Normalized container-relative filename %r → /%s", filename, filename)
            filename = "/" + filename

        if os.path.isabs(filename):
            candidate = Path(filename).resolve()
            # Opportunistically learn Neo's project-slug for this thread so scripts
            # written later can have their relative <slug>/ references rewritten.
            self._record_wrapper(thread_id, candidate)
            if self._is_allowed_path(candidate, ws_resolved):
                file_path = candidate
            else:
                # Backend sent its own container path (e.g. /app/project/src/main.py).
                # Remap to the user's local workspace preserving relative structure.
                file_path = self._remap_to_workspace(candidate, ws_resolved, workdir, strip_project_wrapper=True)
                logger.info("Remapped absolute path %s → %s", filename, file_path)
        else:
            if workdir and os.path.isabs(workdir):
                # Backend supplied an absolute workdir (e.g. /app/project/test_2/demo).
                # Remap it to the local workspace — project wrapper is stripped.
                # e.g. /app/project/test_2/demo → <workspace>/demo
                base = self._remap_to_workspace(Path(workdir).resolve(), ws_resolved, strip_project_wrapper=True, is_workdir=True)
            elif workdir:
                # Relative workdir: first segment is always the project-name wrapper
                # (e.g. "multimodal_rag_0345" or "multimodal_rag_0345/src").
                # Strip the first segment — mirrors how absolute /app/project/{name}/...
                # paths are handled with strip_project_wrapper=True.
                wd_parts = Path(workdir).parts
                rest = Path(*wd_parts[1:]) if len(wd_parts) > 1 else None
                base = ws_resolved / rest if rest else ws_resolved
                if rest:
                    logger.info("Relative workdir: stripped project wrapper %r → base=%s", wd_parts[0], base)
            else:
                base = ws_resolved
            candidate = (base / filename).resolve()
            if not self._is_allowed_path(candidate, ws_resolved):
                logger.warning("Path traversal blocked: %s", filename)
                return {"request_id": request_id, "status": "error", "error": "Path traversal detected"}
            file_path = candidate

        # Rewrite Neo's relative <slug>/ references inside shell scripts. Without this
        # a script like `mkdir -p <slug>/data` creates <workspace>/<slug>/data when the
        # daemon runs it with cwd=<workspace> — the slug was meant relative to Neo's
        # container cwd (/app/<slug>/) and has no meaning on the host.
        if file_path.suffix in (".sh", ".bash") or (isinstance(code, str) and code.startswith("#!")):
            code = self._apply_wrapper_rewrite(code, thread_id)

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

        # Normalize container-relative paths (same logic as _write_code).
        _CONTAINER_REL_PREFIXES = ("app/project/", "app/", "workspace/", "project/")
        if not os.path.isabs(file_path_raw) and any(file_path_raw.startswith(p) for p in _CONTAINER_REL_PREFIXES):
            logger.info("get_file: normalized container-relative path %r → /%s", file_path_raw, file_path_raw)
            file_path_raw = "/" + file_path_raw

        if os.path.isabs(file_path_raw):
            candidate = Path(file_path_raw).resolve()
            self._record_wrapper(thread_id, candidate)
            if self._is_allowed_path(candidate, ws_resolved):
                resolved = candidate
            else:
                # Backend container path (e.g. /app/project/src/file.py) — remap to local workspace
                resolved = self._remap_to_workspace(candidate, ws_resolved, strip_project_wrapper=True)
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

        # Strip relative wrapper references (`cd <slug>`, `mkdir <slug>/data`) for which
        # _remap_command_paths can't help — they're syntactically indistinguishable from
        # real subdirs. We use the slug captured from earlier absolute writes on this thread.
        command_str = self._apply_wrapper_rewrite(command_str, thread_id)

        # Ensure workspace exists before spawning — mirrors npm mkdirSync(safeCwd).
        ws_path.mkdir(parents=True, exist_ok=True)

        # Merge integration env (Anthropic/OpenRouter API keys, HF token, GitHub PAT, ...)
        # into the child process env so Neo tasks inherit the user's credentials
        # without having to re-supply them each run.
        extra_env = self._integrations.env_for_subprocess()
        child_env = os.environ.copy()
        if extra_env:
            child_env.update(extra_env)

        if not detach:
            # Blocking (synchronous) mode — mirrors npm executor.ts hRunSubprocess detach=false.
            # Run command to completion and return stdout/stderr immediately in the response.
            proc = await asyncio.create_subprocess_shell(
                command_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                env=child_env,
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

        job_id = await self._job_manager.create_job(
            command_str, workspace, thread_id or "unknown", extra_env=extra_env or None,
        )
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
                # Backend container path (e.g. /app/project or /app/<wrapper>) — remap to
                # local workspace.  is_workdir=True so a single-segment wrapper like
                # /app/myproj_0001 maps to workspace root rather than a wrapper subfolder.
                target = self._remap_to_workspace(
                    candidate, ws_resolved,
                    strip_project_wrapper=True, is_workdir=True,
                )
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
                # Mirror write_code's wrapper-stripping: Neo always wraps its files under
                # <container_root>/<project-name>/, so `ls /app/<proj>/data/` must resolve
                # to `<workspace>/data/` — not `<workspace>/<proj>/data/`. Without this,
                # write_code lands at `<workspace>/data/x.txt` but Neo's verify subprocess
                # looks at `<workspace>/<proj>/data/x.txt` (wrong) and retries forever.
                remapped = self._remap_to_workspace(path, _ws, strip_project_wrapper=True)
                result_str = str(remapped)
                if trailing and not result_str.endswith('/'):
                    result_str += '/'
                return result_str

            result = re.sub(pattern, _replace, result)
        return result

    def _remap_to_workspace(
        self,
        path: Path,
        workspace: Path,
        workdir_hint: Optional[str] = None,
        *,
        strip_project_wrapper: bool = False,
        is_workdir: bool = False,
    ) -> Path:
        """Remap a backend container path (e.g. /app/project/src/main.py) to the local workspace.

        strip_project_wrapper=True: strips the first segment after any known container
        root (/app/project, /app, /workspace, /project) — that segment is always the
        backend's project-name wrapper (e.g. "agent_session_manager_0930"), not part of
        the file tree.  Use this for write_code, get_file, list_files.

        is_workdir=True: treat a single segment as the project root (maps to workspace).
        is_workdir=False (default): a single segment is kept as a filename component.

        Default (False for both): strips only when the first segment matches the workspace
        folder name.  Used by _remap_command_paths where segments may be real subdirs.
        """
        relative: Optional[Path] = None
        stripable_root = False

        if workdir_hint and os.path.isabs(workdir_hint):
            try:
                relative = path.relative_to(Path(workdir_hint).resolve())
            except ValueError:
                pass

        if relative is None:
            try:
                relative = path.relative_to(Path("/app/project"))
                stripable_root = True
            except ValueError:
                pass

        if relative is None:
            for root in [Path("/app"), Path("/workspace"), Path("/project")]:
                try:
                    relative = path.relative_to(root)
                    stripable_root = True
                    break
                except ValueError:
                    continue

        if relative is None:
            return workspace / path.name

        parts = relative.parts
        if parts and strip_project_wrapper and stripable_root:
            # Strip the project-name wrapper (first segment after the container root).
            # The backend always wraps files under <container_root>/<project-name>/,
            # so the first segment is the project name, not part of the file tree.
            #
            # For filenames (is_workdir=False): only strip when there are 2+ parts
            # (1-segment paths like /app/model.py are filename-at-root, keep them).
            # For workdirs (is_workdir=True): always strip — even a single segment
            # like /app/test_2 means "project root" → workspace.
            should_strip = is_workdir or len(parts) >= 2
            if should_strip:
                ws_name = workspace.parts[-1] if workspace.parts else ""
                logger.info(
                    "Stripping project wrapper %r from %s (local workspace=%r)",
                    parts[0], path, ws_name,
                )
                relative = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        elif parts and workspace.parts and workspace.parts[-1] == parts[0]:
            # Legacy dedup: strip only when workspace name matches the first segment.
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
