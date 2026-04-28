"""
test_system.py — comprehensive test suite for the neo-mcp system.

No network calls required. Covers every module end-to-end:
  ActionHandlers   write_code · get_file · run_subprocess · list_files
                   create_session · unknown action
  JobManager       create · logs · terminate · cleanup
  PathRemapping    _remap_to_workspace · _remap_command_paths
  PathSecurity     traversal · symlink escape · allowed directories
  WorkspaceIso     concurrent threads · no cross-contamination
  BackendPoller    _safe_send retries · _should_accept · register_workspace
  DeploymentId     machine-stable UUID · key-derived · env override

Run with: python3 -m pytest python/tests/test_system.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from neo_mcp.action_handlers import ActionHandlers
from neo_mcp.auth import derive_deployment_id, get_or_create_deployment_id
from neo_mcp.backend_poller import BackendPoller
from neo_mcp.job_manager import JobManager, _Job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def arun(coro):
    return asyncio.run(coro)


def make_ws() -> str:
    return tempfile.mkdtemp(prefix="neo-test-")


def make_handlers(
    workspace: str | None = None,
    thread_workspaces: dict[str, str] | None = None,
) -> tuple[ActionHandlers, str]:
    ws = workspace or make_ws()
    tw = {} if thread_workspaces is None else thread_workspaces
    h = ActionHandlers(
        job_manager=JobManager(),
        default_workspace=ws,
        thread_workspaces=tw,
    )
    return h, ws


def fake_job(
    job_id: str = "j1",
    *,
    exit_code: int | None = 0,
    hours_old: float = 0.0,
    thread_id: str = "t1",
) -> _Job:
    started = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    completed = started if exit_code is not None else None
    return _Job(
        job_id=job_id,
        pid=None,
        command="echo test",
        working_directory="/tmp",
        thread_id=thread_id,
        started_at=started,
        completed_at=completed,
        exit_code=exit_code,
    )


def make_poller_with_mock_send(send_fn=None) -> BackendPoller:
    client = MagicMock()
    client.send_response = send_fn or AsyncMock(return_value=None)
    h, ws = make_handlers()
    return BackendPoller(
        deployment_id="dep-test",
        client=client,
        handlers=h,
        thread_workspaces={},
    )


# ===========================================================================
# PART 1 — write_code
# ===========================================================================

class TestWriteCode(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.h, self.ws = make_handlers(workspace=self.td, thread_workspaces={"t1": self.td})

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def cmd(self, **kw) -> dict:
        return {"action": "write_code", "request_id": "r", "thread_id": "t1", **kw}

    # --- happy path ---

    def test_relative_filename_lands_in_workspace(self):
        r = arun(self.h.handle_command(self.cmd(filename="model.py", code="# hi")))
        self.assertEqual(r["status"], "success")
        self.assertEqual(Path(self.td, "model.py").read_text(), "# hi")

    def test_subdirectory_auto_created(self):
        r = arun(self.h.handle_command(self.cmd(filename="src/models/train.py", code="# train")))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "src/models/train.py").exists())

    def test_overwrite_existing_file(self):
        Path(self.td, "a.py").write_text("old")
        arun(self.h.handle_command(self.cmd(filename="a.py", code="new")))
        self.assertEqual(Path(self.td, "a.py").read_text(), "new")

    def test_empty_string_code_is_valid(self):
        r = arun(self.h.handle_command(self.cmd(filename="empty.py", code="")))
        self.assertEqual(r["status"], "success")
        self.assertEqual(Path(self.td, "empty.py").read_text(), "")

    def test_unicode_content_written(self):
        r = arun(self.h.handle_command(self.cmd(filename="unicode.py", code="# 日本語 αβγ")))
        self.assertEqual(r["status"], "success")
        self.assertEqual(Path(self.td, "unicode.py").read_text(encoding="utf-8"), "# 日本語 αβγ")

    def test_workdir_echoed_in_response(self):
        r = arun(self.h.handle_command(self.cmd(filename="f.py", code="x", workdir="src")))
        self.assertEqual(r["data"]["workdir"], "src")

    def test_no_workdir_gives_empty_string_in_response(self):
        r = arun(self.h.handle_command(self.cmd(filename="f.py", code="x")))
        self.assertEqual(r["data"]["workdir"], "")

    # --- container path remapping ---

    def test_absolute_app_project_remapped(self):
        # /app/project/ is the workspace mount, so paths under it are real user
        # paths — preserve the full subdir structure under the workspace.
        r = arun(self.h.handle_command(self.cmd(filename="/app/project/src/main.py", code="# main")))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "src", "main.py").exists())
        self.assertFalse(Path(self.td, "main.py").exists(),
                         "must NOT strip 'src' — it's a real user subfolder under /app/project/")

    def test_absolute_app_root_remapped(self):
        r = arun(self.h.handle_command(self.cmd(filename="/app/model.py", code="# model")))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "model.py").exists())

    def test_absolute_workspace_root_remapped(self):
        r = arun(self.h.handle_command(self.cmd(filename="/workspace/trainer.py", code="# t")))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "trainer.py").exists())

    def test_absolute_project_root_remapped(self):
        r = arun(self.h.handle_command(self.cmd(filename="/project/runner.py", code="# r")))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "runner.py").exists())

    def test_absolute_workdir_with_relative_filename(self):
        r = arun(self.h.handle_command(
            self.cmd(filename="utils.py", code="# u", workdir="/app/project")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "utils.py").exists())

    def test_relative_workdir_single_segment_stripped(self):
        # Core regression: backend sends workdir="multimodal_rag_0345" (relative, single segment).
        # This is the project-name wrapper — must be stripped so file lands at workspace root.
        r = arun(self.h.handle_command(
            self.cmd(filename="model.py", code="# m", workdir="multimodal_rag_0345")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "model.py").exists(), "file must land at workspace root")
        self.assertFalse(Path(self.td, "multimodal_rag_0345", "model.py").exists(),
                         "must NOT create a project-name subfolder")

    def test_relative_workdir_with_subdir_preserves_subdir(self):
        # Multi-segment relative workdir: first segment is project name, rest is real subdir.
        # e.g. "multimodal_rag_0345/src" → base = workspace/src
        r = arun(self.h.handle_command(
            self.cmd(filename="train.py", code="# t", workdir="multimodal_rag_0345/src")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "src", "train.py").exists(),
                        "subdir after project wrapper must be preserved")
        self.assertFalse(Path(self.td, "multimodal_rag_0345").exists(),
                         "project-name folder must not be created")

    def test_container_relative_filename_normalized(self):
        # Backend sometimes sends "app/project/myproj/model.py" (no leading '/').
        # Normalized to /app/project/myproj/model.py — under /app/project/ (workspace
        # mount) the subdir structure is preserved as a real user path.
        r = arun(self.h.handle_command(
            self.cmd(filename="app/project/myproj/model.py", code="# m")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "myproj", "model.py").exists(),
                        "container-relative filename must preserve subdirs under /app/project/")
        self.assertFalse(Path(self.td, "app").exists(),
                         "must NOT create 'app/' subfolder in workspace")

    def test_container_relative_filename_app_only(self):
        # "app/model.py" (no leading '/') → treated as /app/model.py → remapped to workspace/model.py
        r = arun(self.h.handle_command(
            self.cmd(filename="app/model.py", code="# a")
        ))
        self.assertEqual(r["status"], "success")
        self.assertFalse(Path(self.td, "app").exists(),
                         "must NOT create 'app/' subfolder in workspace")

    def test_dedup_workspace_name_in_path(self):
        # workspace is self.td (last component e.g. neo-test-XXXX)
        ws_name = Path(self.td).name
        r = arun(self.h.handle_command(
            self.cmd(filename=f"/app/project/{ws_name}/model.py", code="# model")
        ))
        self.assertEqual(r["status"], "success")
        # Must land at workspace/model.py, NOT workspace/<ws_name>/model.py
        self.assertTrue(Path(self.td, "model.py").exists())
        self.assertFalse(Path(self.td, ws_name, "model.py").exists())

    def test_app_root_wrapper_stripped(self):
        # Regression: backend sends /app/<wrapper>/file.py (NOT /app/project/<wrapper>/...).
        # Wrapper must be stripped so file lands at workspace root, not in a wrapper subfolder.
        # This is the exact pattern from the user's daemon log:
        #   /app/multiagent_showcase_setup_0931/agents/research_agent.py
        wrapper = "multiagent_showcase_setup_0931"
        r = arun(self.h.handle_command(
            self.cmd(filename=f"/app/{wrapper}/agents/research_agent.py", code="# r")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "agents", "research_agent.py").exists(),
                        "file must land at workspace/agents/, no wrapper subfolder")
        self.assertFalse(Path(self.td, wrapper).exists(),
                         f"must NOT create wrapper folder '{wrapper}' in workspace")

    def test_app_root_wrapper_with_nested_package(self):
        # Real-world case from daemon log:
        #   /app/rag_preparation_tool_0933/ragprep/__init__.py
        # Wrapper = rag_preparation_tool_0933 (stripped); ragprep is a real package dir (kept).
        wrapper = "rag_preparation_tool_0933"
        r = arun(self.h.handle_command(
            self.cmd(filename=f"/app/{wrapper}/ragprep/__init__.py", code="")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "ragprep", "__init__.py").exists())
        self.assertFalse(Path(self.td, wrapper).exists())

    def test_app_root_workdir_with_subdir(self):
        # workdir=/app/<wrapper>/sub + relative filename → file lands at workspace/sub/
        wrapper = "myproj_0001"
        r = arun(self.h.handle_command(
            self.cmd(filename="train.py", code="# t", workdir=f"/app/{wrapper}/src")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "src", "train.py").exists())
        self.assertFalse(Path(self.td, wrapper).exists())

    def test_app_project_user_subfolder_preserved(self):
        # Regression: when the user asks Neo to build inside `<workspace>/demo/`,
        # Neo emits `/app/project/demo/<file>` paths. Pre-fix, the daemon stripped
        # `demo/` (treating it as a wrapper) and files landed at workspace root.
        # New semantic: /app/project/ is the workspace mount, so `demo/` is a
        # real user subfolder and must be preserved.
        r = arun(self.h.handle_command(
            self.cmd(filename="/app/project/demo/data_loader.py", code="# loader")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "demo", "data_loader.py").exists(),
                        "file must land in user subfolder, not workspace root")
        self.assertFalse(Path(self.td, "data_loader.py").exists(),
                         "must NOT strip user subfolder name from /app/project/<sub>/...")

    def test_app_project_user_subfolder_workdir_preserved(self):
        # Same regression via workdir route: workdir=/app/project/demo + relative
        # filename → file lands in <ws>/demo/, not at workspace root.
        r = arun(self.h.handle_command(
            self.cmd(filename="train.py", code="# t", workdir="/app/project/demo")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "demo", "train.py").exists())
        self.assertFalse(Path(self.td, "train.py").exists())

    # --- error cases ---

    def test_missing_filename_returns_error(self):
        r = arun(self.h.handle_command(self.cmd(code="x")))
        self.assertEqual(r["status"], "error")
        self.assertIn("filename", r["error"].lower())

    def test_missing_code_returns_error(self):
        r = arun(self.h.handle_command(self.cmd(filename="f.py")))
        self.assertEqual(r["status"], "error")

    def test_relative_traversal_blocked(self):
        r = arun(self.h.handle_command(self.cmd(filename="../../evil.sh", code="rm -rf /")))
        self.assertEqual(r["status"], "error")
        self.assertFalse(Path("/evil.sh").exists())

    def test_deep_traversal_blocked(self):
        r = arun(self.h.handle_command(self.cmd(filename="../" * 10 + "etc/passwd", code="evil")))
        self.assertEqual(r["status"], "error")

    def test_no_thread_id_uses_default_workspace(self):
        ws2 = make_ws()
        try:
            h = ActionHandlers(JobManager(), default_workspace=ws2, thread_workspaces={})
            r = arun(h.handle_command({"action": "write_code", "request_id": "r",
                                       "filename": "default.py", "code": "# default"}))
            self.assertEqual(r["status"], "success")
            self.assertTrue(Path(ws2, "default.py").exists())
        finally:
            shutil.rmtree(ws2, ignore_errors=True)


# ===========================================================================
# PART 2 — get_file
# ===========================================================================

class TestGetFile(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.h, self.ws = make_handlers(workspace=self.td, thread_workspaces={"t1": self.td})

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def write(self, filename: str, content: str) -> None:
        p = Path(self.td, filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def cmd(self, **kw) -> dict:
        return {"action": "get_file", "request_id": "r", "thread_id": "t1", **kw}

    def test_relative_path_reads_correctly(self):
        self.write("data.csv", "col1,col2\n1,2")
        r = arun(self.h.handle_command(self.cmd(file_path="data.csv")))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["file_content"], "col1,col2\n1,2")

    def test_absolute_workspace_path_reads(self):
        self.write("notes.txt", "hello")
        r = arun(self.h.handle_command(self.cmd(file_path=str(Path(self.td, "notes.txt")))))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["file_content"], "hello")

    def test_container_path_remapped_and_read(self):
        self.write("model.py", "# model")
        r = arun(self.h.handle_command(self.cmd(file_path="/app/project/model.py")))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["file_content"], "# model")

    def test_workspace_container_path_remapped(self):
        self.write("eval.py", "# eval")
        r = arun(self.h.handle_command(self.cmd(file_path="/workspace/eval.py")))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["file_content"], "# eval")

    def test_app_root_wrapper_remapped(self):
        # Symmetric with write_code: backend sends /app/<wrapper>/file.py for get_file too.
        # Wrapper must be stripped so the read finds the file in workspace root.
        self.write("config.yaml", "key: value")
        r = arun(self.h.handle_command(
            self.cmd(file_path="/app/multiagent_showcase_setup_0931/config.yaml")
        ))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["file_content"], "key: value")

    def test_missing_file_returns_error(self):
        r = arun(self.h.handle_command(self.cmd(file_path="nonexistent.py")))
        self.assertEqual(r["status"], "error")
        self.assertIn("not found", r["error"].lower())

    def test_missing_file_path_field_returns_error(self):
        r = arun(self.h.handle_command(self.cmd()))
        self.assertEqual(r["status"], "error")

    def test_relative_traversal_blocked(self):
        r = arun(self.h.handle_command(self.cmd(file_path="../../etc/passwd")))
        self.assertEqual(r["status"], "error")

    def test_absolute_path_outside_workspace_blocked(self):
        r = arun(self.h.handle_command(self.cmd(file_path="/etc/hostname")))
        # Must never read /etc/hostname directly — either blocked or "not found"
        self.assertEqual(r["status"], "error")

    def test_write_then_read_roundtrip(self):
        arun(self.h.handle_command({
            "action": "write_code", "request_id": "w", "thread_id": "t1",
            "filename": "roundtrip.py", "code": "# roundtrip",
        }))
        r = arun(self.h.handle_command(self.cmd(file_path="roundtrip.py")))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["file_content"], "# roundtrip")

    def test_unicode_content_returned_correctly(self):
        self.write("utf8.py", "# 中文 emoji 🚀")
        r = arun(self.h.handle_command(self.cmd(file_path="utf8.py")))
        self.assertEqual(r["status"], "success")
        self.assertIn("🚀", r["data"]["file_content"])


# ===========================================================================
# PART 3 — run_subprocess
# ===========================================================================

class TestRunSubprocess(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.td = make_ws()
        self.h, self.ws = make_handlers(workspace=self.td, thread_workspaces={"t1": self.td})

    async def asyncTearDown(self):
        # Kill any still-running subprocesses.
        jm = self.h._job_manager
        for jid in list(jm._jobs):
            jm.terminate_job(jid)
        await asyncio.sleep(0)
        shutil.rmtree(self.td, ignore_errors=True)

    def cmd(self, **kw) -> dict:
        return {"action": "run_subprocess", "request_id": "r", "thread_id": "t1", **kw}

    # --- detached (default) ---

    async def test_detached_returns_job_id(self):
        r = await self.h.handle_command(self.cmd(command="echo hi"))
        self.assertEqual(r["status"], "success")
        self.assertIn("job_id", r["data"])
        self.assertTrue(r["data"]["detached"])

    async def test_detached_job_output_via_get_job_status(self):
        start = await self.h.handle_command(self.cmd(command="echo neo-marker"))
        job_id = start["data"]["job_id"]
        # Poll until completed (max 3 s)
        for _ in range(30):
            s = await self.h.handle_command({
                "action": "get_job_status", "request_id": "r2",
                "thread_id": "t1", "job_id": job_id,
            })
            if s["data"]["exit_code"] is not None:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(s["data"]["exit_code"], 0)
        self.assertIn("neo-marker", s["data"]["stdout"])

    async def test_detached_missing_command_returns_error(self):
        r = await self.h.handle_command(self.cmd())
        self.assertEqual(r["status"], "error")
        self.assertIn("command", r["error"].lower())

    async def test_command_from_payload(self):
        r = await self.h.handle_command({
            "action": "run_subprocess", "request_id": "r", "thread_id": "t1",
            "payload": {"command": "echo from-payload"},
        })
        self.assertEqual(r["status"], "success")
        self.assertIn("job_id", r["data"])

    # --- blocking (detach=False) ---

    async def test_blocking_returns_inline_stdout(self):
        r = await self.h.handle_command(
            self.cmd(command="echo inline-test", detach=False)
        )
        self.assertEqual(r["status"], "completed")
        self.assertFalse(r["data"]["detached"])
        self.assertTrue(r["data"]["completed"])
        self.assertIn("inline-test", r["data"]["stdout"])
        self.assertEqual(r["data"]["exit_code"], 0)

    async def test_blocking_captures_stderr(self):
        r = await self.h.handle_command(
            self.cmd(command="echo err >&2", detach=False)
        )
        self.assertEqual(r["status"], "completed")
        self.assertIn("err", r["data"]["stderr"])

    async def test_blocking_nonzero_exit_returns_error_status(self):
        r = await self.h.handle_command(
            self.cmd(command="exit 1", detach=False)
        )
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["data"]["exit_code"], 1)

    async def test_blocking_zero_exit_returns_completed_status(self):
        r = await self.h.handle_command(
            self.cmd(command="true", detach=False)
        )
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["data"]["exit_code"], 0)

    # --- cd-guard (Bug B safety net) ---

    async def test_cd_guard_runs_command_at_workspace(self):
        # The `cd <workspace> && ` prefix injected before the spawn must leave
        # cwd at the workspace, so `pwd` echoes the workspace path. Resolve()
        # to handle macOS /tmp → /private/tmp symlink redirection.
        r = await self.h.handle_command(
            self.cmd(command="pwd", detach=False)
        )
        self.assertEqual(r["status"], "completed")
        self.assertEqual(
            r["data"]["stdout"].strip(),
            str(Path(self.td).resolve()),
        )

    async def test_cd_guard_short_circuits_bad_cd_chain(self):
        # If Neo emits `cd <wrong-slug> && rm -rf …` and no recorded wrapper
        # rewrites <wrong-slug>, the cd-guard's outer `cd <workspace> &&`
        # succeeds, the inner `cd /nonexistent` fails, and `&&` short-circuits
        # the destructive tail. The marker must NOT exist after this.
        marker = Path(tempfile.gettempdir()) / f"neo-cdguard-{uuid.uuid4().hex}.marker"
        if marker.exists():
            marker.unlink()
        try:
            r = await self.h.handle_command(self.cmd(
                command=f"cd /does-not-exist-12345 && touch {marker}",
                detach=False,
            ))
            self.assertEqual(r["status"], "error")  # nonzero exit from failed cd
            self.assertFalse(
                marker.exists(),
                f"cd-guard failed to short-circuit: {marker} was created",
            )
        finally:
            if marker.exists():
                marker.unlink()

    async def test_cd_guard_quotes_workspace_with_spaces(self):
        # Workspace paths can contain spaces — the cd-guard must shlex.quote
        # them or the spawn dies with `cd: too many arguments`.
        ws_with_space = tempfile.mkdtemp(prefix="neo test ")
        try:
            h, _ = make_handlers(
                workspace=ws_with_space,
                thread_workspaces={"t-space": ws_with_space},
            )
            r = await h.handle_command({
                "action": "run_subprocess", "request_id": "r",
                "thread_id": "t-space", "command": "pwd", "detach": False,
            })
            self.assertEqual(r["status"], "completed")
            self.assertEqual(
                r["data"]["stdout"].strip(),
                str(Path(ws_with_space).resolve()),
            )
        finally:
            shutil.rmtree(ws_with_space, ignore_errors=True)

    # --- preflight check ---

    async def test_missing_tmp_script_fails_fast(self):
        r = await self.h.handle_command(
            self.cmd(command="bash /tmp/bash_exec_deadbeef01234567.sh")
        )
        self.assertEqual(r["status"], "error")
        self.assertIn("Script not found", r["error"])

    # --- path remapping in command string ---

    async def test_container_path_in_command_remapped(self):
        # Backend sends `ls /app/project` — daemon must remap to workspace
        r = await self.h.handle_command(
            self.cmd(command="ls /app/project", detach=False)
        )
        # Command should run (cwd = workspace, ls workspace works)
        self.assertIn(r["status"], ["completed", "error"])  # may fail if cwd empty, but not crash

    # --- terminate ---

    async def test_terminate_unknown_job_returns_false(self):
        r = await self.h.handle_command({
            "action": "terminate_job", "request_id": "r", "thread_id": "t1",
            "job_id": "no-such-job",
        })
        self.assertEqual(r["status"], "error")

    async def test_terminate_running_job_returns_success(self):
        start = await self.h.handle_command(self.cmd(command="sleep 60"))
        job_id = start["data"]["job_id"]
        r = await self.h.handle_command({
            "action": "terminate_job", "request_id": "r2", "thread_id": "t1",
            "job_id": job_id,
        })
        self.assertEqual(r["status"], "success")
        self.assertTrue(r["data"]["terminated"])


# ===========================================================================
# PART 4 — JobManager
# ===========================================================================

class TestJobManager(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.td = make_ws()
        self.jm = JobManager()

    async def asyncTearDown(self):
        # Kill any still-running subprocesses so asyncio doesn't wait for them.
        for jid in list(self.jm._jobs):
            self.jm.terminate_job(jid)
        await asyncio.sleep(0)
        shutil.rmtree(self.td, ignore_errors=True)

    async def test_create_job_returns_job_id(self):
        jid = await self.jm.create_job("echo test", self.td, "t1")
        self.assertIsInstance(jid, str)
        self.assertGreater(len(jid), 8)

    async def test_get_job_logs_initially_running(self):
        jid = await self.jm.create_job("sleep 60", self.td, "t1")
        logs = self.jm.get_job_logs(jid)
        self.assertIsNotNone(logs)
        self.assertEqual(logs["job_id"], jid)
        self.assertIn(logs["status"], ["running", "completed"])

    async def test_get_job_logs_after_completion(self):
        jid = await self.jm.create_job("echo done-marker", self.td, "t1")
        for _ in range(30):
            logs = self.jm.get_job_logs(jid)
            if logs["exit_code"] is not None:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(logs["exit_code"], 0)
        self.assertEqual(logs["status"], "completed")
        self.assertIn("done-marker", logs["stdout"])

    async def test_get_job_logs_unknown_id_returns_none(self):
        self.assertIsNone(self.jm.get_job_logs("no-such-job"))

    async def test_terminate_job_running(self):
        jid = await self.jm.create_job("sleep 120", self.td, "t1")
        result = self.jm.terminate_job(jid)
        self.assertTrue(result)

    async def test_terminate_job_unknown_returns_false(self):
        self.assertFalse(self.jm.terminate_job("no-such"))

    async def test_terminate_already_completed_returns_true(self):
        jid = await self.jm.create_job("true", self.td, "t1")
        for _ in range(30):
            if self.jm.get_job_logs(jid)["exit_code"] is not None:
                break
            await asyncio.sleep(0.1)
        # Terminating an already-complete job should succeed (True, not error)
        self.assertTrue(self.jm.terminate_job(jid))

    def test_cleanup_evicts_old_completed_jobs(self):
        jm = JobManager()
        old = fake_job("old", exit_code=0, hours_old=25)
        recent = fake_job("recent", exit_code=0, hours_old=1)
        running = fake_job("running", exit_code=None, hours_old=48)
        jm._jobs = {"old": old, "recent": recent, "running": running}
        jm.cleanup_old_jobs()
        self.assertNotIn("old", jm._jobs)
        self.assertIn("recent", jm._jobs)
        self.assertIn("running", jm._jobs)

    def test_cleanup_empty_registry_is_noop(self):
        jm = JobManager()
        jm.cleanup_old_jobs()  # must not raise
        self.assertEqual(len(jm._jobs), 0)

    def test_get_logs_returns_none_after_cleanup(self):
        jm = JobManager()
        old = fake_job("old-gone", exit_code=0, hours_old=25)
        jm._jobs = {"old-gone": old}
        jm.cleanup_old_jobs()
        self.assertIsNone(jm.get_job_logs("old-gone"))


# ===========================================================================
# PART 5 — list_files
# ===========================================================================

class TestListFiles(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.h, self.ws = make_handlers(workspace=self.td, thread_workspaces={"t1": self.td})
        # Build test tree: src/train.py, README.md, .hidden, venv/lib/pkg.py
        (Path(self.td) / "src").mkdir()
        (Path(self.td) / "src" / "train.py").write_text("# train")
        (Path(self.td) / "README.md").write_text("# readme")
        (Path(self.td) / ".hidden").write_text("secret")
        (Path(self.td) / "venv" / "lib").mkdir(parents=True)
        (Path(self.td) / "venv" / "lib" / "pkg.py").write_text("# pkg")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def cmd(self, **kw) -> dict:
        return {"action": "list_files", "request_id": "r", "thread_id": "t1", **kw}

    def test_lists_files_in_workspace(self):
        r = arun(self.h.handle_command(self.cmd()))
        self.assertEqual(r["status"], "success")
        self.assertIn("train.py", r["data"]["stdout"])
        self.assertIn("README.md", r["data"]["stdout"])

    def test_hidden_files_excluded_by_default(self):
        r = arun(self.h.handle_command(self.cmd()))
        self.assertNotIn(".hidden", r["data"]["stdout"])

    def test_hidden_files_included_when_requested(self):
        r = arun(self.h.handle_command(self.cmd(include_hidden=True)))
        self.assertIn(".hidden", r["data"]["stdout"])

    def test_skip_dirs_not_recursed(self):
        r = arun(self.h.handle_command(self.cmd()))
        # venv dir itself appears but its contents are not recursed
        self.assertNotIn("pkg.py", r["data"]["stdout"])

    def test_max_depth_limits_recursion(self):
        r = arun(self.h.handle_command(self.cmd(max_depth=1)))
        # depth=1 visits workspace children only — src/ dir but not src/train.py
        self.assertNotIn("train.py", r["data"]["stdout"])

    def test_missing_directory_returns_error(self):
        r = arun(self.h.handle_command(self.cmd(directory="/no/such/dir")))
        self.assertEqual(r["status"], "error")

    def test_file_count_matches_stdout_lines(self):
        r = arun(self.h.handle_command(self.cmd()))
        lines = [l for l in r["data"]["stdout"].split("\n") if l]
        self.assertEqual(r["data"]["file_count"], len(lines))

    def test_container_directory_remapped(self):
        r = arun(self.h.handle_command(self.cmd(directory="/app/project")))
        self.assertEqual(r["status"], "success")
        self.assertIn("README.md", r["data"]["stdout"])

    def test_app_root_wrapper_directory_remapped(self):
        # Backend may send directory=/app/<wrapper> for list_files. Wrapper must be
        # stripped (is_workdir=True semantics) so the listing reflects workspace contents.
        r = arun(self.h.handle_command(self.cmd(directory="/app/multiagent_showcase_setup_0931")))
        self.assertEqual(r["status"], "success")
        self.assertIn("README.md", r["data"]["stdout"])

    def test_dirs_appear_before_files(self):
        r = arun(self.h.handle_command(self.cmd()))
        lines = [l for l in r["data"]["stdout"].split("\n") if l]
        dir_lines = [l for l in lines if "|d|" in l]
        file_lines = [l for l in lines if "|f|" in l]
        if dir_lines and file_lines:
            self.assertLess(
                r["data"]["stdout"].index(dir_lines[0]),
                r["data"]["stdout"].index(file_lines[0]),
            )


# ===========================================================================
# PART 6 — create_session
# ===========================================================================

class TestCreateSession(unittest.TestCase):

    def setUp(self):
        self.h, _ = make_handlers()

    def test_with_explicit_session_id(self):
        r = arun(self.h.handle_command({
            "action": "create_session", "request_id": "r", "session_id": "sess-123"
        }))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["coding_session_id"], "sess-123")

    def test_with_payload_session_id(self):
        r = arun(self.h.handle_command({
            "action": "create_session", "request_id": "r",
            "payload": {"session_id": "payload-sess"},
        }))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"]["coding_session_id"], "payload-sess")

    def test_auto_generates_uuid_when_omitted(self):
        r = arun(self.h.handle_command({"action": "create_session", "request_id": "r"}))
        self.assertEqual(r["status"], "success")
        sid = r["data"]["coding_session_id"]
        self.assertIsInstance(sid, str)
        self.assertGreater(len(sid), 8)


# ===========================================================================
# PART 7 — dispatch routing / unknown actions
# ===========================================================================

class TestDispatch(unittest.TestCase):

    def setUp(self):
        self.h, _ = make_handlers()

    def test_unknown_action_returns_error(self):
        r = arun(self.h.handle_command({"action": "fly_to_mars", "request_id": "r"}))
        self.assertEqual(r["status"], "error")
        self.assertIn("fly_to_mars", r["error"])

    def test_empty_action_returns_error(self):
        r = arun(self.h.handle_command({"action": "", "request_id": "r"}))
        self.assertEqual(r["status"], "error")

    def test_missing_action_returns_error(self):
        r = arun(self.h.handle_command({"request_id": "r"}))
        self.assertEqual(r["status"], "error")

    def test_request_id_echoed_in_response(self):
        r = arun(self.h.handle_command({"action": "create_session", "request_id": "req-xyz"}))
        self.assertEqual(r["request_id"], "req-xyz")

    def test_all_seven_actions_are_routable(self):
        """Smoke: every known action reaches a handler and does not return 'unknown action'."""
        known = ["create_session", "write_code", "get_file", "run_subprocess",
                 "get_job_status", "terminate_job", "list_files"]
        for action in known:
            r = arun(self.h.handle_command({"action": action, "request_id": "r"}))
            self.assertNotEqual(r.get("error", ""), f"Unknown action: {action}", msg=action)


# ===========================================================================
# PART 8 — _remap_to_workspace
# ===========================================================================

class TestRemapToWorkspace(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.h, _ = make_handlers(workspace=self.td)
        self.ws = Path(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def remap(self, path: str, workdir: str | None = None) -> str:
        return str(self.h._remap_to_workspace(Path(path), self.ws, workdir))

    def test_app_project_path(self):
        self.assertEqual(self.remap("/app/project/src/main.py"), str(self.ws / "src/main.py"))

    def test_app_root_path(self):
        self.assertEqual(self.remap("/app/model.py"), str(self.ws / "model.py"))

    def test_workspace_root_path(self):
        self.assertEqual(self.remap("/workspace/trainer.py"), str(self.ws / "trainer.py"))

    def test_project_root_path(self):
        self.assertEqual(self.remap("/project/runner.py"), str(self.ws / "runner.py"))

    def test_exact_app_project_root(self):
        self.assertEqual(self.remap("/app/project"), str(self.ws))

    def test_dedup_workspace_name_in_path(self):
        ws_name = self.ws.name
        result = self.remap(f"/app/project/{ws_name}/model.py")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_no_dedup_when_names_differ(self):
        result = self.remap("/app/project/src/model.py")
        self.assertEqual(result, str(self.ws / "src/model.py"))

    def test_nested_path_preserved(self):
        result = self.remap("/app/project/a/b/c/d.py")
        self.assertEqual(result, str(self.ws / "a/b/c/d.py"))

    def test_workdir_hint_priority(self):
        # workdir=/app/project/sub, path=/app/project/sub/model.py → relative=model.py
        result = self.remap("/app/project/sub/model.py", workdir="/app/project/sub")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_unknown_root_falls_back_to_filename(self):
        result = self.remap("/some/unknown/root/file.py")
        self.assertEqual(result, str(self.ws / "file.py"))

    # ------------------------------------------------------------------
    # strip_project_wrapper=True — the fix for mismatched project names
    # ------------------------------------------------------------------

    def remap_strip(self, path: str, workdir: str | None = None) -> str:
        """Remap with strip_project_wrapper=True (filename context — is_workdir=False)."""
        return str(self.h._remap_to_workspace(Path(path), self.ws, workdir, strip_project_wrapper=True))

    def remap_strip_wd(self, path: str) -> str:
        """Remap with strip_project_wrapper=True, is_workdir=True (workdir remap context)."""
        return str(self.h._remap_to_workspace(Path(path), self.ws, None, strip_project_wrapper=True, is_workdir=True))

    def test_strip_wrapper_matching_name_same_result(self):
        """When workspace name matches the first segment under /app/project/, legacy dedup strips it."""
        ws_name = self.ws.name
        result = self.remap_strip(f"/app/project/{ws_name}/model.py")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_app_project_does_not_strip_user_subfolder(self):
        """Regression: /app/project/<X>/ is the workspace mount — <X> is a user subfolder.

        Previously this stripped <X> as if it were a Neo wrapper, causing files
        the user asked to live in `demo/` or `rag/` to land at the workspace
        root instead. Now preserved.
        """
        result = self.remap_strip("/app/project/demo/data.csv")
        self.assertEqual(result, str(self.ws / "demo" / "data.csv"))

    def test_app_project_preserves_nested_user_path(self):
        """Deep path under /app/project/: full structure preserved (no strip)."""
        result = self.remap_strip("/app/project/test_2/src/utils.py")
        self.assertEqual(result, str(self.ws / "test_2" / "src" / "utils.py"))

    def test_strip_wrapper_filename_at_container_root_kept(self):
        """Single-segment after /app/project/ is treated as a filename, not stripped."""
        result = self.remap_strip("/app/project/model.py")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_app_project_workdir_single_segment_preserved(self):
        """workdir=/app/project/<X> (is_workdir=True) preserves <X> as a user subfolder."""
        result = self.remap_strip_wd("/app/project/demo")
        self.assertEqual(result, str(self.ws / "demo"))

    def test_app_project_workdir_subdir_preserved(self):
        """workdir=/app/project/<X>/<Y> → workspace/<X>/<Y> (full path preserved)."""
        result = self.remap_strip_wd("/app/project/demo/src")
        self.assertEqual(result, str(self.ws / "demo" / "src"))

    def test_strip_wrapper_workdir_hint_takes_priority(self):
        """workdir_hint is still applied before any stripping."""
        result = self.remap_strip("/app/project/sub/model.py", workdir="/app/project/sub")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_strip_wrapper_single_segment_at_app_root_kept(self):
        """Single-segment under /app, /workspace, /project is the filename — not stripped."""
        self.assertEqual(self.remap_strip("/app/model.py"), str(self.ws / "model.py"))
        self.assertEqual(self.remap_strip("/workspace/train.py"), str(self.ws / "train.py"))
        self.assertEqual(self.remap_strip("/project/run.sh"), str(self.ws / "run.sh"))

    def test_strip_wrapper_exact_app_project_root(self):
        """Exact /app/project with no file still maps to workspace."""
        self.assertEqual(self.remap_strip("/app/project"), str(self.ws))

    # ------------------------------------------------------------------
    # Regression: backend sends /app/<wrapper>/... (not /app/project/<wrapper>/...)
    # The wrapper must be stripped from all known container roots, not just /app/project.
    # ------------------------------------------------------------------

    def test_strip_wrapper_app_root_with_wrapper(self):
        """/app/<wrapper>/file.py — wrapper stripped (the headline regression)."""
        result = self.remap_strip("/app/multiagent_showcase_setup_0931/agents/research_agent.py")
        self.assertEqual(result, str(self.ws / "agents/research_agent.py"))

    def test_strip_wrapper_app_root_nested_subdirs_preserved(self):
        """/app/<wrapper>/<deep>/<sub>/file.py — only wrapper stripped, deep subdirs kept."""
        result = self.remap_strip("/app/rag_preparation_tool_0933/ragprep/ingestor.py")
        self.assertEqual(result, str(self.ws / "ragprep/ingestor.py"))

    def test_strip_wrapper_workspace_root_with_wrapper(self):
        """/workspace/<wrapper>/file.py — wrapper stripped (same generalization)."""
        result = self.remap_strip("/workspace/myproj_0001/src/main.py")
        self.assertEqual(result, str(self.ws / "src/main.py"))

    def test_strip_wrapper_project_root_with_wrapper(self):
        """/project/<wrapper>/file.py — wrapper stripped."""
        result = self.remap_strip("/project/myproj_0001/src/main.py")
        self.assertEqual(result, str(self.ws / "src/main.py"))

    def test_strip_wrapper_app_root_workdir_single_segment(self):
        """workdir=/app/<wrapper> (is_workdir=True) maps to workspace root."""
        result = self.remap_strip_wd("/app/myproj_0001")
        self.assertEqual(result, str(self.ws))

    def test_strip_wrapper_app_root_workdir_subdir(self):
        """workdir=/app/<wrapper>/sub → workspace/sub."""
        result = self.remap_strip_wd("/app/myproj_0001/sub")
        self.assertEqual(result, str(self.ws / "sub"))

    def test_legacy_dedup_unaffected_by_app_root_change(self):
        """When strip_project_wrapper=False (e.g. _remap_command_paths), /app/<seg>/file
        keeps original behavior: legacy dedup only when first segment matches workspace name."""
        # Workspace name doesn't match → first segment kept
        self.assertEqual(self.remap("/app/foo/bar.py"), str(self.ws / "foo/bar.py"))
        # Workspace name matches first segment → first segment stripped (legacy dedup)
        ws_name = self.ws.name
        self.assertEqual(self.remap(f"/app/{ws_name}/bar.py"), str(self.ws / "bar.py"))


# ===========================================================================
# PART 9 — _remap_command_paths
# ===========================================================================

class TestRemapCommandPaths(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.h, _ = make_handlers(workspace=self.td)
        self.ws = Path(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def remap(self, cmd: str) -> str:
        return self.h._remap_command_paths(cmd, self.ws)

    def test_remaps_app_project_in_ls(self):
        self.assertEqual(self.remap(f"ls /app/project/foo"), f"ls {self.ws}/foo")

    def test_remaps_bare_app_project(self):
        self.assertEqual(self.remap("ls /app/project"), f"ls {self.ws}")

    def test_remaps_app_root(self):
        self.assertEqual(self.remap("cat /app/model.py"), f"cat {self.ws}/model.py")

    def test_remaps_workspace_root(self):
        self.assertEqual(self.remap("python /workspace/train.py"), f"python {self.ws}/train.py")

    def test_remaps_project_root(self):
        self.assertEqual(self.remap("bash /project/run.sh"), f"bash {self.ws}/run.sh")

    def test_remaps_multiple_paths_in_command(self):
        # Neo always wraps under /app/<project-name>/ — the wrapper is stripped by
        # remapCommandPaths (mirrors write_code) so `src` and `dst` land as subdirs.
        result = self.remap("cp /app/myproj/src/a.py /app/myproj/dst/a.py")
        self.assertIn(str(self.ws / "src/a.py"), result)
        self.assertIn(str(self.ws / "dst/a.py"), result)

    def test_strips_project_wrapper_for_verify_subprocess(self):
        # Regression: Neo verifies writes via `test -f /app/<proj>/data/x.txt`. The
        # subprocess remap must match write_code's wrapper-stripping, else verify
        # looks at <ws>/<proj>/data/x.txt (doesn't exist) and Neo loops forever.
        result = self.remap('test -f "/app/rag_pipeline/data/ml_docs.txt"')
        self.assertIn(str(self.ws / "data/ml_docs.txt"), result)
        self.assertNotIn("rag_pipeline", result)

    def test_leaves_non_container_paths_unchanged(self):
        cmd = "echo hello && ls /tmp/logs"
        self.assertEqual(self.remap(cmd), cmd)

    def test_remaps_cd_chained_command(self):
        result = self.remap("cd /app/project/src && python train.py")
        self.assertIn(str(self.ws / "src"), result)
        self.assertIn("python train.py", result)

    def test_preserves_trailing_slash(self):
        result = self.remap("ls /app/project/")
        self.assertTrue(result.endswith("/"))

    def test_dedup_workspace_name_in_command(self):
        ws_name = self.ws.name
        result = self.remap(f"ls /app/project/{ws_name}/src/")
        self.assertIn(str(self.ws / "src/"), result)
        self.assertNotIn(f"{ws_name}/{ws_name}", result)


# ===========================================================================
# PART 10 — path security (_is_allowed_path)
# ===========================================================================

class TestPathSecurity(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.h, _ = make_handlers(workspace=self.td)
        self.ws = Path(self.td).resolve()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def allowed(self, path: str) -> bool:
        return self.h._is_allowed_path(Path(path).resolve(), self.ws)

    def test_workspace_root_allowed(self):
        self.assertTrue(self.allowed(self.td))

    def test_file_in_workspace_allowed(self):
        self.assertTrue(self.allowed(str(Path(self.td) / "model.py")))

    def test_deep_subdir_in_workspace_allowed(self):
        self.assertTrue(self.allowed(str(Path(self.td) / "a" / "b" / "c" / "d.py")))

    def test_tmp_path_allowed(self):
        self.assertTrue(self.allowed("/tmp/script.sh"))

    def test_etc_passwd_blocked(self):
        self.assertFalse(self.allowed("/etc/passwd"))

    def test_root_blocked(self):
        self.assertFalse(self.allowed("/"))

    def test_parent_of_workspace_blocked(self):
        # Use a non-/tmp fake workspace so the parent is clearly not a TMP_DIR.
        fake_ws = Path("/home/neo-test-parent-check/myproject")
        h, _ = make_handlers(workspace=str(fake_ws))
        self.assertFalse(h._is_allowed_path(fake_ws.parent.resolve(), fake_ws.resolve()))

    def test_sibling_of_workspace_blocked(self):
        fake_ws = Path("/home/neo-test-sibling-check/myproject")
        sibling = fake_ws.parent / "other-project"
        h, _ = make_handlers(workspace=str(fake_ws))
        self.assertFalse(h._is_allowed_path(sibling.resolve(), fake_ws.resolve()))


# ===========================================================================
# PART 11 — symlink escape
# ===========================================================================

class TestSymlinkEscape(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.h, self.ws = make_handlers(workspace=self.td, thread_workspaces={"t1": self.td})
        self.symlink = os.path.join(self.td, "outside-link")
        os.symlink("/etc", self.symlink)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_write_via_symlink_is_blocked(self):
        r = arun(self.h.handle_command({
            "action": "write_code", "request_id": "r", "thread_id": "t1",
            "filename": "outside-link/passwd", "code": "evil",
        }))
        self.assertEqual(r["status"], "error")
        self.assertNotEqual(Path("/etc/passwd").read_text(), "evil")

    def test_absolute_through_symlink_not_written_to_etc(self):
        target = os.path.join(self.td, "outside-link", "neo-test.conf")
        r = arun(self.h.handle_command({
            "action": "write_code", "request_id": "r", "thread_id": "t1",
            "filename": target, "code": "evil",
        }))
        # Either blocked (error) or remapped into workspace — /etc must be clean
        self.assertFalse(Path("/etc/neo-test.conf").exists())
        if r["status"] == "success":
            self.assertTrue(r["data"]["file_path"].startswith(self.td))

    def test_get_file_via_symlink_blocked_or_remapped(self):
        r = arun(self.h.handle_command({
            "action": "get_file", "request_id": "r", "thread_id": "t1",
            "file_path": "outside-link/hostname",
        }))
        if r["status"] == "success":
            self.assertTrue(r["data"]["file_path"].startswith(self.td))


# ===========================================================================
# PART 12 — workspace isolation (concurrent threads)
# ===========================================================================

class TestWorkspaceIsolation(unittest.TestCase):

    def setUp(self):
        self.workspaces = [make_ws() for _ in range(3)]

    def tearDown(self):
        for ws in self.workspaces:
            shutil.rmtree(ws, ignore_errors=True)

    def make_isolated_handler(self, thread_id: str, workspace: str) -> ActionHandlers:
        return ActionHandlers(JobManager(), workspace, {thread_id: workspace})

    def test_sequential_writes_land_in_correct_workspaces(self):
        for i, ws in enumerate(self.workspaces):
            h = self.make_isolated_handler(f"t{i}", ws)
            arun(h.handle_command({
                "action": "write_code", "request_id": "r", "thread_id": f"t{i}",
                "filename": f"file_{i}.py", "code": f"# thread {i}",
            }))
        for i, ws in enumerate(self.workspaces):
            self.assertTrue(Path(ws, f"file_{i}.py").exists())
            for j in range(3):
                if j != i:
                    self.assertFalse(Path(ws, f"file_{j}.py").exists())

    def test_concurrent_writes_no_cross_contamination(self):
        tw = {f"t{i}": ws for i, ws in enumerate(self.workspaces)}
        h = ActionHandlers(JobManager(), self.workspaces[0], tw)

        async def write_all():
            await asyncio.gather(*[
                h.handle_command({
                    "action": "write_code", "request_id": "r", "thread_id": f"t{i}",
                    "filename": f"concurrent_{i}.py", "code": f"# {i}",
                })
                for i in range(3)
            ])

        arun(write_all())
        for i, ws in enumerate(self.workspaces):
            self.assertTrue(Path(ws, f"concurrent_{i}.py").exists())
            for j in range(3):
                if j != i:
                    self.assertFalse(Path(ws, f"concurrent_{j}.py").exists())

    def test_concurrent_many_files_per_thread(self):
        tw = {f"t{i}": ws for i, ws in enumerate(self.workspaces)}
        h = ActionHandlers(JobManager(), self.workspaces[0], tw)

        async def write_all():
            ops = []
            for i in range(3):
                for f in range(5):
                    ops.append(h.handle_command({
                        "action": "write_code", "request_id": "r",
                        "thread_id": f"t{i}", "filename": f"file_t{i}_f{f}.py",
                        "code": f"# t{i} f{f}",
                    }))
            await asyncio.gather(*ops)

        arun(write_all())
        for i, ws in enumerate(self.workspaces):
            for f in range(5):
                self.assertTrue(Path(ws, f"file_t{i}_f{f}.py").exists())

    def test_container_paths_per_workspace_isolated(self):
        tw = {f"t{i}": ws for i, ws in enumerate(self.workspaces)}
        h = ActionHandlers(JobManager(), self.workspaces[0], tw)
        for i in range(3):
            arun(h.handle_command({
                "action": "write_code", "request_id": "r", "thread_id": f"t{i}",
                "filename": f"/app/project/model_{i}.py", "code": f"# model_{i}",
            }))
        for i, ws in enumerate(self.workspaces):
            self.assertTrue(Path(ws, f"model_{i}.py").exists())

    def test_unknown_thread_refuses_write_and_reports_error(self):
        """Fail-loud: a command for an unregistered thread must NOT write to
        the default workspace. Prior behaviour was silent fallback which
        could drop files into the wrong project folder (or the neo-mcp repo
        itself). Shortened registration-wait keeps the test fast."""
        import neo_mcp.action_handlers as ah_mod
        orig = ah_mod._WORKSPACE_REGISTRATION_WAIT_SECONDS
        ah_mod._WORKSPACE_REGISTRATION_WAIT_SECONDS = 0.02
        try:
            h = ActionHandlers(JobManager(), self.workspaces[0], {})
            r = arun(h.handle_command({
                "action": "write_code", "request_id": "r", "thread_id": "unknown",
                "filename": "fallback.py", "code": "# fallback",
            }))
            self.assertEqual(r["status"], "error")
            self.assertIn("No workspace registered", r["error"])
            self.assertIn("unknown", r["error"])
            self.assertFalse(
                Path(self.workspaces[0], "fallback.py").exists(),
                "file must NOT have leaked into default workspace",
            )
        finally:
            ah_mod._WORKSPACE_REGISTRATION_WAIT_SECONDS = orig


# ===========================================================================
# PART 13 — BackendPoller._safe_send (retry logic)
#
# Root cause of the Cursor RAG stuck-task incident:
# _safe_send had no retries in the published version. One failed HTTP POST
# meant the backend never received the ACK and stalled permanently.
# ===========================================================================

class TestSafeSend(unittest.IsolatedAsyncioTestCase):

    def make_poller(self, send_fn) -> BackendPoller:
        return make_poller_with_mock_send(send_fn)

    async def test_succeeds_on_first_attempt(self):
        fn = AsyncMock(return_value=None)
        p = self.make_poller(fn)
        await p._safe_send({"request_id": "r1", "status": "success"})
        self.assertEqual(fn.call_count, 1)

    async def test_retries_on_failure_succeeds_second(self):
        count = 0
        async def flaky(*_a, **_kw):
            nonlocal count; count += 1
            if count == 1: raise OSError("reset")
        p = self.make_poller(flaky)
        await p._safe_send({"request_id": "r1", "status": "success"})
        self.assertEqual(count, 2)

    async def test_retries_on_failure_succeeds_third(self):
        count = 0
        async def flaky(*_a, **_kw):
            nonlocal count; count += 1
            if count < 3: raise OSError("transient")
        p = self.make_poller(flaky)
        await p._safe_send({"request_id": "r1", "status": "success"})
        self.assertEqual(count, 3)

    async def test_exhausts_all_3_and_does_not_raise(self):
        """If all 3 retries fail, daemon must survive (not crash)."""
        count = 0
        async def always_fail(*_a, **_kw):
            nonlocal count; count += 1
            raise OSError("down")
        p = self.make_poller(always_fail)
        await p._safe_send({"request_id": "r1", "status": "success"})  # must not raise
        self.assertEqual(count, 3)

    async def test_no_extra_calls_after_success(self):
        fn = AsyncMock(return_value=None)
        p = self.make_poller(fn)
        for _ in range(5):
            await p._safe_send({"request_id": "r", "status": "ok"})
        self.assertEqual(fn.call_count, 5)


# ===========================================================================
# PART 14 — BackendPoller._should_accept (thread status gate)
# ===========================================================================

class TestThreadStatusGate(unittest.TestCase):

    def setUp(self):
        self.p = make_poller_with_mock_send()

    def test_unknown_thread_accepted(self):
        self.assertTrue(self.p._should_accept("thread-never-set"))

    def test_running_thread_accepted(self):
        self.p.set_thread_status("t1", "RUNNING")
        self.assertTrue(self.p._should_accept("t1"))

    def test_paused_thread_accepted(self):
        self.p.set_thread_status("t2", "PAUSED")
        self.assertTrue(self.p._should_accept("t2"))

    def test_terminated_thread_rejected(self):
        self.p.set_thread_status("t3", "TERMINATED")
        self.assertFalse(self.p._should_accept("t3"))

    def test_failed_thread_rejected(self):
        self.p.set_thread_status("t4", "FAILED")
        self.assertFalse(self.p._should_accept("t4"))

    def test_stopped_thread_rejected(self):
        self.p.set_thread_status("t5", "STOPPED")
        self.assertFalse(self.p._should_accept("t5"))

    def test_status_update_changes_acceptance(self):
        self.p.set_thread_status("t6", "RUNNING")
        self.assertTrue(self.p._should_accept("t6"))
        self.p.set_thread_status("t6", "TERMINATED")
        self.assertFalse(self.p._should_accept("t6"))


# ===========================================================================
# PART 15 — BackendPoller workspace registration
# ===========================================================================

class TestPollerWorkspaceRegistration(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self.p = make_poller_with_mock_send()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_register_updates_in_memory_immediately(self):
        self.p.register_thread_workspace("t1", self.td)
        self.assertEqual(self.p._thread_workspaces["t1"], self.td)

    def test_unregistered_thread_not_in_workspaces(self):
        self.assertNotIn("t-unknown", self.p._thread_workspaces)

    def test_overwrite_registration_takes_effect(self):
        td2 = make_ws()
        try:
            self.p.register_thread_workspace("t1", self.td)
            self.p.register_thread_workspace("t1", td2)
            self.assertEqual(self.p._thread_workspaces["t1"], td2)
        finally:
            shutil.rmtree(td2, ignore_errors=True)

    def test_multiple_threads_isolated_in_memory(self):
        td2 = make_ws()
        try:
            self.p.register_thread_workspace("tA", self.td)
            self.p.register_thread_workspace("tB", td2)
            self.assertEqual(self.p._thread_workspaces["tA"], self.td)
            self.assertEqual(self.p._thread_workspaces["tB"], td2)
        finally:
            shutil.rmtree(td2, ignore_errors=True)


# ===========================================================================
# PART 15b — register_thread_wrapper — pre-seed slugs (Bug A primary fix)
# ===========================================================================

class TestRegisterThreadWrapper(unittest.TestCase):
    """Wrapper-hint flow: bridge supplies the project slug at submit time so
    the daemon can strip it from the very first relative-path command,
    instead of waiting for an absolute container path to teach it the slug
    (which is what produces the empty <slug>/ folder at workspace root)."""

    def setUp(self):
        self.td = make_ws()
        self.p = make_poller_with_mock_send()
        # Workspace must be registered for _save_thread_workspaces to persist
        # the wrapper alongside it.
        self.p.register_thread_workspace("t1", self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_register_seeds_in_memory_immediately(self):
        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.assertEqual(
            self.p._thread_wrappers["t1"],
            ["rag_system_langchain_0937"],
        )

    def test_register_appends_distinct_slugs(self):
        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.p.register_thread_wrapper("t1", "kimi-rag-api")
        self.assertEqual(
            self.p._thread_wrappers["t1"],
            ["rag_system_langchain_0937", "kimi-rag-api"],
        )

    def test_register_dedups_repeat(self):
        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.assertEqual(
            self.p._thread_wrappers["t1"],
            ["rag_system_langchain_0937"],
        )

    def test_register_empty_wrapper_is_noop(self):
        self.p.register_thread_wrapper("t1", "")
        self.assertNotIn("t1", self.p._thread_wrappers)

    def test_register_persists_to_disk(self):
        import json
        from neo_mcp.paths import THREAD_WORKSPACES_FILE

        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.p.register_thread_wrapper("t1", "kimi-rag-api")
        raw = json.loads(THREAD_WORKSPACES_FILE.read_text())
        self.assertEqual(
            raw["t1"]["wrappers"],
            ["rag_system_langchain_0937", "kimi-rag-api"],
        )

    def test_handlers_see_registered_wrappers(self):
        # The shared mutable dict pattern means ActionHandlers reads the same
        # list register_thread_wrapper writes — apply_wrapper_rewrite must
        # therefore strip the seeded slug without ever seeing an absolute path.
        self.p._handlers._thread_wrappers = self.p._thread_wrappers
        self.p._handlers._thread_workspaces["t1"] = self.td
        self.p.register_thread_wrapper("t1", "kimi-rag-api")
        rewritten = self.p._handlers._apply_wrapper_rewrite(
            "mkdir -p kimi-rag-api/plans", "t1",
        )
        self.assertEqual(rewritten, "mkdir -p plans")


# ===========================================================================
# PART 15c — Wrapper persistence across daemon restart (Bug A safety net)
# ===========================================================================

class TestThreadWrapperPersistence(unittest.TestCase):
    """Wrappers must survive daemon restart so a re-attached thread doesn't
    re-open the wrapper-learn race after a process bounce."""

    def setUp(self):
        self.td = make_ws()
        self.p = make_poller_with_mock_send()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_load_wrappers_round_trip(self):
        self.p.register_thread_workspace("t1", self.td)
        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.p.register_thread_wrapper("t1", "kimi-rag-api")
        loaded = BackendPoller._load_thread_wrappers()
        self.assertEqual(
            loaded["t1"],
            ["rag_system_langchain_0937", "kimi-rag-api"],
        )

    def test_load_wrappers_missing_field_returns_empty(self):
        # Legacy entry without a `wrappers` field must not appear in the loaded
        # wrapper map (and must not crash). Existing thread-workspaces.json
        # files predate the wrappers field.
        self.p.register_thread_workspace("t1", self.td)
        loaded = BackendPoller._load_thread_wrappers()
        self.assertNotIn("t1", loaded)

    def test_load_wrappers_tolerates_string_form(self):
        # Defensively accept a single-string `wrappers` value — wrap it in a list.
        import json
        from neo_mcp.paths import THREAD_WORKSPACES_FILE

        THREAD_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
        THREAD_WORKSPACES_FILE.write_text(json.dumps({
            "t1": {
                "workspace": self.td,
                "updated_at": int(time.time()),
                "wrappers": "single_slug_form",
            },
        }))
        loaded = BackendPoller._load_thread_wrappers()
        self.assertEqual(loaded["t1"], ["single_slug_form"])

    def test_load_wrappers_skips_non_string_entries(self):
        import json
        from neo_mcp.paths import THREAD_WORKSPACES_FILE

        THREAD_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
        THREAD_WORKSPACES_FILE.write_text(json.dumps({
            "t1": {
                "workspace": self.td,
                "updated_at": int(time.time()),
                "wrappers": ["good_slug", 42, "", "another_good"],
            },
        }))
        loaded = BackendPoller._load_thread_wrappers()
        self.assertEqual(loaded["t1"], ["good_slug", "another_good"])


# ===========================================================================
# PART 16 — Deployment ID
# ===========================================================================

class TestSecretKeyWhitespace(unittest.TestCase):
    """Tolerate trailing whitespace in NEO_SECRET_KEY.

    A trailing space sneaks in via Claude Code's MCP env block in
    ~/.claude.json (and similar config sources). httpx then rejects the
    Authorization header as ``Illegal header value`` and every poll fails
    silently — opaque connectivity outage from a typo. Strip at both
    boundaries: env read AND BackendClient construction.
    """

    def setUp(self):
        from neo_mcp.auth import get_secret_key as _gs
        from neo_mcp.backend_client import BackendClient as _BC
        self.get_secret_key = _gs
        self.BackendClient = _BC
        self._orig = os.environ.get("NEO_SECRET_KEY")

    def tearDown(self):
        _restore("NEO_SECRET_KEY", self._orig)

    def test_trailing_space_stripped_from_env(self):
        os.environ["NEO_SECRET_KEY"] = "sk-v1-abc "
        self.assertEqual(self.get_secret_key(), "sk-v1-abc")

    def test_leading_space_stripped_from_env(self):
        os.environ["NEO_SECRET_KEY"] = "  sk-v1-abc"
        self.assertEqual(self.get_secret_key(), "sk-v1-abc")

    def test_empty_after_strip_returns_none(self):
        os.environ["NEO_SECRET_KEY"] = "   "
        self.assertIsNone(self.get_secret_key())

    def test_unset_returns_none(self):
        os.environ.pop("NEO_SECRET_KEY", None)
        self.assertIsNone(self.get_secret_key())

    def test_backend_client_strips_token_at_construction(self):
        c = self.BackendClient(auth_token="sk-v1-abc \t\n")
        self.assertEqual(c._auth_token, "sk-v1-abc")
        # Authorization header must contain no trailing whitespace —
        # otherwise httpx raises InvalidHeader and the poll loop dies.
        self.assertEqual(c._headers()["Authorization"], "Bearer sk-v1-abc")

    def test_backend_client_update_token_strips(self):
        c = self.BackendClient(auth_token="sk-v1-abc")
        c.update_token("sk-v1-def \r\n")
        self.assertEqual(c._auth_token, "sk-v1-def")

    def test_backend_client_handles_none_token_gracefully(self):
        # Constructed elsewhere with empty/None? Don't crash on .strip().
        c = self.BackendClient(auth_token="")
        self.assertEqual(c._auth_token, "")


class TestDeploymentId(unittest.TestCase):

    def setUp(self):
        self.td = make_ws()
        self._orig_dep_id = os.environ.get("NEO_DEPLOYMENT_ID")
        self._orig_mode = os.environ.get("NEO_DEPLOYMENT_ID_MODE")
        self._uuid_file = os.path.join(self.td, "standalone_deployment_id")
        # Patch auth.STANDALONE_UUID_FILE — auth.py uses the imported binding,
        # so we must patch the name in the auth module directly.
        import neo_mcp.auth as auth_module
        self._auth_module = auth_module
        self._orig_uuid_file = auth_module.STANDALONE_UUID_FILE
        auth_module.STANDALONE_UUID_FILE = Path(self._uuid_file)

    def tearDown(self):
        self._auth_module.STANDALONE_UUID_FILE = self._orig_uuid_file
        _restore("NEO_DEPLOYMENT_ID", self._orig_dep_id)
        _restore("NEO_DEPLOYMENT_ID_MODE", self._orig_mode)
        shutil.rmtree(self.td, ignore_errors=True)

    def test_creates_file_on_first_call(self):
        self.assertFalse(os.path.exists(self._uuid_file))
        dep_id = get_or_create_deployment_id("sk-v1-test")
        self.assertTrue(os.path.exists(self._uuid_file))
        self.assertEqual(Path(self._uuid_file).read_text().strip(), dep_id)

    def test_stable_across_calls(self):
        id1 = get_or_create_deployment_id("sk-v1-test")
        id2 = get_or_create_deployment_id("sk-v1-test")
        self.assertEqual(id1, id2)

    def test_not_derived_from_key_by_default(self):
        dep_id = get_or_create_deployment_id("sk-v1-somekey")
        derived = derive_deployment_id("sk-v1-somekey")
        self.assertNotEqual(dep_id, derived)

    def test_explicit_env_override_takes_priority(self):
        os.environ["NEO_DEPLOYMENT_ID"] = "explicit-override-id"
        result = get_or_create_deployment_id("sk-v1-test")
        self.assertEqual(result, "explicit-override-id")

    def test_key_derived_mode_is_deterministic(self):
        os.environ["NEO_DEPLOYMENT_ID_MODE"] = "key-derived"
        id1 = get_or_create_deployment_id("sk-v1-mykey")
        id2 = get_or_create_deployment_id("sk-v1-mykey")
        self.assertEqual(id1, id2)
        self.assertEqual(id1, derive_deployment_id("sk-v1-mykey"))

    def test_two_different_keys_give_different_derived_ids(self):
        id1 = derive_deployment_id("sk-v1-key1")
        id2 = derive_deployment_id("sk-v1-key2")
        self.assertNotEqual(id1, id2)

    def test_derived_id_is_valid_uuid_format(self):
        import re
        dep_id = derive_deployment_id("sk-v1-format-test")
        self.assertRegex(dep_id, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_machine_uuid_is_stable_regardless_of_key_change(self):
        id1 = get_or_create_deployment_id("sk-v1-first")
        id2 = get_or_create_deployment_id("sk-v1-second")
        self.assertEqual(id1, id2)


class TestRelativeWrapperStrip(unittest.IsolatedAsyncioTestCase):
    """Strip Neo's relative <slug>/ references from scripts and commands.

    Neo writes shell scripts whose bodies contain `mkdir -p <slug>/data` and commands
    like `cd <slug> && python main.py`, assuming cwd=/app/<slug>/ on its container.
    Our daemon runs with cwd=<workspace>, so the slug prefix must be stripped or we
    get <workspace>/<slug>/data instead of <workspace>/data.
    """

    def setUp(self):
        self.td = make_ws()
        self.tid = "t-wrap-1"
        # Register t-wrap-1 so the fail-loud _workspace_for has a mapping.
        # Prior to the fail-loud change these tests relied on the silent
        # fallback to default_workspace; registering the thread is the
        # explicit form of what they actually needed.
        self.h, _ = make_handlers(
            workspace=self.td,
            thread_workspaces={self.tid: self.td},
        )
        self.ws = Path(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_extract_wrapper_from_app_path(self):
        self.assertEqual(
            self.h._extract_wrapper(Path("/app/movie_recommender_system_1703/data/x.txt")),
            "movie_recommender_system_1703",
        )
        # /app/project/<X>/ is the workspace mount — <X> is a user subfolder, NOT
        # a wrapper. Must return None so it isn't auto-recorded for stripping.
        self.assertIsNone(self.h._extract_wrapper(Path("/app/project/foo/bar.py")))

    def test_extract_wrapper_returns_none_for_non_container_paths(self):
        self.assertIsNone(self.h._extract_wrapper(Path("/tmp/script.sh")))
        self.assertIsNone(self.h._extract_wrapper(Path("/app/bare.py")))  # no wrapper after /app/

    def test_record_wrapper_first_abs_write_captures_slug(self):
        self.h._record_wrapper(self.tid, Path("/app/my_proj_0001/data/a.txt"))
        self.assertEqual(self.h._thread_wrappers[self.tid], ["my_proj_0001"])

    def test_record_wrapper_appends_additional_slug(self):
        # Multi-slug tracking — Bug B fix: when Neo's plan-text references a
        # second slug different from the first absolute-path-derived one
        # (e.g. recorded "rag_system_langchain_0937" but plan says
        # "kimi-rag-api"), both must accumulate so the strip pass catches
        # either alias.
        self.h._record_wrapper(self.tid, Path("/app/my_proj_0001/data/a.txt"))
        self.h._record_wrapper(self.tid, Path("/app/different_proj_9999/data/b.txt"))
        self.assertEqual(
            self.h._thread_wrappers[self.tid],
            ["my_proj_0001", "different_proj_9999"],
        )

    def test_record_wrapper_dedups_repeated_slug(self):
        self.h._record_wrapper(self.tid, Path("/app/my_proj_0001/data/a.txt"))
        self.h._record_wrapper(self.tid, Path("/app/my_proj_0001/data/b.txt"))
        self.assertEqual(self.h._thread_wrappers[self.tid], ["my_proj_0001"])

    def test_strip_prefix_in_mkdir(self):
        result = self.h._strip_wrapper_prefixes("mkdir -p my_proj_0001/data", "my_proj_0001")
        self.assertEqual(result, "mkdir -p data")

    def test_strip_bare_wrapper_in_cd(self):
        result = self.h._strip_wrapper_prefixes("cd my_proj_0001 && python main.py", "my_proj_0001")
        self.assertEqual(result, "cd . && python main.py")

    def test_no_strip_when_wrapper_is_substring(self):
        # `my_my_proj_0001` is a different identifier — must NOT be stripped.
        result = self.h._strip_wrapper_prefixes("ls my_my_proj_0001/foo", "my_proj_0001")
        self.assertEqual(result, "ls my_my_proj_0001/foo")

    def test_write_code_rewrites_shell_script(self):
        import asyncio
        # Prime the wrapper via an earlier absolute write.
        asyncio.run(self.h._write_code({
            "request_id": "r1", "thread_id": self.tid,
            "filename": "/app/my_proj_0001/data/seed.txt", "code": "seed",
        }))
        # Now write a script whose body uses the relative wrapper.
        asyncio.run(self.h._write_code({
            "request_id": "r2", "thread_id": self.tid,
            "filename": ".tmp/neo_exec.sh",
            "code": "mkdir -p my_proj_0001/data && cd my_proj_0001 && ls",
        }))
        content = (self.ws / ".tmp" / "neo_exec.sh").read_text()
        self.assertEqual(content, "mkdir -p data && cd . && ls")

    def test_run_subprocess_strips_wrapper_from_command(self):
        self.h._thread_wrappers[self.tid] = ["movie_recommender_system_1703"]
        # Inspect the rewrite via the public helper since _run_subprocess spawns a job.
        rewritten = self.h._apply_wrapper_rewrite(
            "mkdir -p movie_recommender_system_1703/data",
            self.tid,
        )
        self.assertEqual(rewritten, "mkdir -p data")

    # Regression: pipx 0.4.34 on hosts with a real /app/ produced `/app/.` for
    # `target = '/app/<wrapper>'` (no trailing /), causing scripts to walk the
    # host's /app/ instead of the user's workspace. Step 1 of the strip must
    # remap absolute <root>/<wrapper> paths to the workspace.
    def test_strip_absolute_container_path_no_trailing_slash(self):
        ws = Path("/tmp/host_ws")
        text = "target = '/app/minimal_sentiment_classifier_1004'"
        result = self.h._strip_wrapper_prefixes(
            text, "minimal_sentiment_classifier_1004", ws,
        )
        self.assertEqual(result, "target = '/tmp/host_ws'")

    def test_strip_absolute_container_path_with_subpath(self):
        ws = Path("/tmp/host_ws")
        text = "ls /app/my_proj_0001/src/main.py"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
        self.assertEqual(result, "ls /tmp/host_ws/src/main.py")

    def test_strip_absolute_path_all_wrapper_roots(self):
        # Wrapper-extracting roots: /app/, /workspace/, /project/. /app/project/ is
        # excluded (workspace mount — its first segment is a user subfolder).
        ws = Path("/tmp/host_ws")
        for root in ("/app", "/workspace", "/project"):
            text = f"cat {root}/my_proj_0001/data.txt"
            result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
            self.assertEqual(
                result, "cat /tmp/host_ws/data.txt",
                f"failed for container root {root}",
            )

    def test_strip_absolute_path_app_project_preserves_user_subfolder(self):
        # /app/project/ is workspace mount — first segment is preserved as a user
        # subfolder, even when it matches a registered wrapper. Step 0 swaps
        # /app/project → <workspace>; step 1's lookbehind on the wrapper match
        # then prevents stripping the now-leading <workspace>/<wrapper>/ segment.
        ws = Path("/tmp/host_ws")
        text = "cat /app/project/my_proj_0001/data.txt"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
        self.assertEqual(result, "cat /tmp/host_ws/my_proj_0001/data.txt")

    def test_strip_absolute_does_not_match_similar_name(self):
        # /app/my_proj_0001_backup must NOT be rewritten (different name).
        ws = Path("/tmp/host_ws")
        text = "cat /app/my_proj_0001_backup/x.txt"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
        self.assertEqual(result, "cat /app/my_proj_0001_backup/x.txt")

    def test_strip_absolute_app_project_is_workspace_mount(self):
        # /app/project/ is the workspace mount — `/app/project/<X>/foo` becomes
        # `<workspace>/<X>/foo` (preserve user subfolder). Step 0 of the rewrite
        # handles the `/app/project` → `<workspace>` swap independently of any
        # wrapper, so registered wrappers don't strip user subfolder names.
        ws = Path("/tmp/host_ws")
        text = "ls /app/project/my_proj_0001/foo"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
        self.assertEqual(result, "ls /tmp/host_ws/my_proj_0001/foo")

    def test_strip_no_workspace_leaves_absolute_paths_untouched(self):
        # Without workspace, step 1 is skipped. The tightened lookbehind in
        # steps 2/3 also excludes `/`, so `/app/<wrapper>` is left alone rather
        # than mangled to `/app/.` (which was the pre-fix bug). Callers that
        # need absolute remap must pass workspace.
        text = "target = '/app/my_proj_0001'"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001")
        self.assertEqual(result, "target = '/app/my_proj_0001'")

    def test_strip_no_workspace_still_rewrites_relative(self):
        # Relative refs (no leading /) still get stripped/replaced without workspace.
        self.assertEqual(
            self.h._strip_wrapper_prefixes("mkdir -p my_proj_0001/data", "my_proj_0001"),
            "mkdir -p data",
        )
        self.assertEqual(
            self.h._strip_wrapper_prefixes("cd my_proj_0001 && ls", "my_proj_0001"),
            "cd . && ls",
        )

    def test_strip_relative_still_works_with_workspace(self):
        ws = Path("/tmp/host_ws")
        text = "mkdir -p my_proj_0001/data && cd my_proj_0001"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
        self.assertEqual(result, "mkdir -p data && cd .")

    def test_apply_wrapper_rewrite_uses_thread_workspace(self):
        self.h._thread_wrappers[self.tid] = ["my_proj_0001"]
        result = self.h._apply_wrapper_rewrite(
            "target = '/app/my_proj_0001'", self.tid,
        )
        # _apply_wrapper_rewrite resolves the workspace, so symlink-free paths on
        # Linux are unchanged but /tmp → /private/tmp on macOS.
        ws_resolved = self.ws.resolve()
        self.assertEqual(result, f"target = '{ws_resolved}'")

    # --- multi-slug stripping (Bug B) ---

    def test_strip_multiple_wrappers_each_alias_rewritten(self):
        # Neo records the internal slug from absolute paths AND a plan-text
        # alias via wrapper_hint. Both must strip from a single command.
        result = self.h._strip_wrapper_prefixes(
            "cd kimi-rag-api && cd rag_system_langchain_0937 && pwd",
            ["rag_system_langchain_0937", "kimi-rag-api"],
        )
        self.assertEqual(result, "cd . && cd . && pwd")

    def test_strip_multiple_wrappers_apply_via_thread_dict(self):
        self.h._thread_wrappers[self.tid] = [
            "rag_system_langchain_0937", "kimi-rag-api",
        ]
        rewritten = self.h._apply_wrapper_rewrite(
            "mkdir -p kimi-rag-api/data && cp rag_system_langchain_0937/x .",
            self.tid,
        )
        self.assertEqual(rewritten, "mkdir -p data && cp x .")

    def test_strip_wrappers_accepts_legacy_single_string(self):
        # Backwards-compat: the API still accepts a bare string for callers
        # that only know about one slug.
        self.assertEqual(
            self.h._strip_wrapper_prefixes("cd my_proj_0001 && ls", "my_proj_0001"),
            "cd . && ls",
        )

    def test_strip_wrappers_dedups_iterable(self):
        # Passing the same slug twice doesn't double-substitute.
        self.assertEqual(
            self.h._strip_wrapper_prefixes(
                "cd my_proj_0001 && ls",
                ["my_proj_0001", "my_proj_0001"],
            ),
            "cd . && ls",
        )


class TestWorkspaceSelfGuard(unittest.TestCase):
    """Reject a workspace that equals the neo-mcp server source tree itself."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_plain_dir_is_allowed(self):
        from neo_mcp.server import _workspace_is_mcp_self
        self.assertFalse(_workspace_is_mcp_self(self._tmp))

    def test_mcp_repo_is_rejected(self):
        from neo_mcp.server import _workspace_is_mcp_self
        marker = Path(self._tmp) / "python" / "src" / "neo_mcp" / "server.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("# stub")
        self.assertTrue(_workspace_is_mcp_self(self._tmp))

    def test_nonexistent_path_is_allowed(self):
        from neo_mcp.server import _workspace_is_mcp_self
        self.assertFalse(_workspace_is_mcp_self("/does/not/exist"))

    def test_submit_task_returns_error_for_self_repo(self):
        from neo_mcp.server import _submit_task
        marker = Path(self._tmp) / "python" / "src" / "neo_mcp" / "server.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("# stub")

        client = MagicMock()
        client.init_chat = AsyncMock()
        poller = MagicMock()

        result = asyncio.run(
            _submit_task(client, "dep-id", poller, self._tmp, {"message": "hi", "workspace": self._tmp})
        )
        self.assertIn("error", result)
        self.assertIn("neo-mcp server's own source tree", result["error"])
        client.init_chat.assert_not_called()


def _restore(key: str, val: str | None) -> None:
    if val is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = val


# ---------------------------------------------------------------------------
# Integrations (GitHub, HuggingFace, Anthropic, OpenRouter)
# ---------------------------------------------------------------------------

from neo_mcp.integrations import IntegrationManager, PROVIDERS, ValidationError
import neo_mcp.integrations.manager as _int_manager_mod
import neo_mcp.integrations.secret_store as _secret_store_mod
from neo_mcp.integrations.secret_store import FileStore, KeyringStore, get_secret_store
from neo_mcp.integrations._fsutil import atomic_write_secret, file_lock
import neo_mcp.integrations.providers.anthropic as _prov_anthropic
import neo_mcp.integrations.providers.openrouter as _prov_openrouter
import neo_mcp.integrations.providers.huggingface as _prov_hf
import neo_mcp.integrations.providers.github as _prov_github


def _make_fake_keyring():
    """In-memory keyring backend for tests — no OS integration needed.

    Built as a factory so ``keyring.backend.KeyringBackend`` is imported only
    when the test actually runs (keyring is an optional dep at runtime but
    always installed for tests).
    """
    import keyring.backend
    import keyring.errors

    class _FakeKeyring(keyring.backend.KeyringBackend):
        priority = 1

        def __init__(self) -> None:
            super().__init__()
            self._store: dict[tuple[str, str], str] = {}

        def set_password(self, service: str, username: str, password: str) -> None:
            self._store[(service, username)] = password

        def get_password(self, service: str, username: str) -> str | None:
            return self._store.get((service, username))

        def delete_password(self, service: str, username: str) -> None:
            if (service, username) not in self._store:
                raise keyring.errors.PasswordDeleteError(username)
            del self._store[(service, username)]

    return _FakeKeyring()


class TestIntegrationRegistry(unittest.TestCase):
    """Schema validation rules per provider."""

    def test_all_four_providers_registered(self):
        self.assertEqual(
            sorted(PROVIDERS.keys()),
            ["anthropic", "github", "huggingface", "openrouter"],
        )

    def test_github_validates_pat_prefix(self):
        mgr = IntegrationManager(metadata_file=Path(make_ws()) / "meta.json")
        with self.assertRaises(ValidationError):
            mgr._validate(PROVIDERS["github"], {"pat": "not_a_github_pat"})
        # A ghp_ prefixed PAT is accepted by validation (write_secret not called here).
        mgr._validate(PROVIDERS["github"], {"pat": "ghp_abcDEF123_valid"})
        mgr._validate(PROVIDERS["github"], {"pat": "github_pat_abc123_xyz"})

    def test_anthropic_requires_sk_ant_prefix(self):
        mgr = IntegrationManager(metadata_file=Path(make_ws()) / "meta.json")
        with self.assertRaises(ValidationError):
            mgr._validate(PROVIDERS["anthropic"], {"api_key": "sk-not-anthropic"})
        mgr._validate(PROVIDERS["anthropic"], {"api_key": "sk-ant-abc123XYZ-_"})

    def test_openrouter_requires_sk_or_prefix(self):
        mgr = IntegrationManager(metadata_file=Path(make_ws()) / "meta.json")
        with self.assertRaises(ValidationError):
            mgr._validate(PROVIDERS["openrouter"], {"api_key": "sk-ant-xxxxx"})
        mgr._validate(PROVIDERS["openrouter"], {"api_key": "sk-or-abc123_xyz"})

    def test_huggingface_requires_hf_prefix(self):
        mgr = IntegrationManager(metadata_file=Path(make_ws()) / "meta.json")
        with self.assertRaises(ValidationError):
            mgr._validate(PROVIDERS["huggingface"], {"token": "not_hf_prefixed"})
        mgr._validate(PROVIDERS["huggingface"], {"token": "hf_abcDEF123"})
        # Real HF tokens can contain underscores and dashes — regression guard
        # against over-strict validation (caught in E2E run against a token
        # like "hf_realistic_token_xyz789").
        mgr._validate(PROVIDERS["huggingface"], {"token": "hf_some_token_with_under_scores"})
        mgr._validate(PROVIDERS["huggingface"], {"token": "hf_some-token-with-dashes"})

    def test_missing_required_field_raises(self):
        mgr = IntegrationManager(metadata_file=Path(make_ws()) / "meta.json")
        with self.assertRaises(ValidationError):
            mgr._validate(PROVIDERS["anthropic"], {})  # missing api_key
        with self.assertRaises(ValidationError):
            mgr._validate(PROVIDERS["anthropic"], {"api_key": ""})


class _IntegrationFixture(unittest.TestCase):
    """Base class: redirects every provider's file path into a per-test tmpdir."""

    def setUp(self):
        self.td = Path(make_ws())
        self.meta = self.td / "integrations.json"

        self._orig_backend = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)
        self._orig_int_dir = _secret_store_mod.INTEGRATIONS_DIR
        self._orig_hf_token = _prov_hf.TOKEN_FILE
        self._orig_gh_creds = _prov_github.CREDENTIALS_FILE

        _secret_store_mod.INTEGRATIONS_DIR = self.td / "integrations"
        _prov_hf.TOKEN_FILE = self.td / "cache_hf" / "token"
        _prov_github.CREDENTIALS_FILE = self.td / "git-credentials"

    def tearDown(self):
        _secret_store_mod.INTEGRATIONS_DIR = self._orig_int_dir
        _prov_hf.TOKEN_FILE = self._orig_hf_token
        _prov_github.CREDENTIALS_FILE = self._orig_gh_creds
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig_backend)
        shutil.rmtree(self.td, ignore_errors=True)

    def secret_env_file(self, provider: str) -> Path:
        return self.td / "integrations" / f"{provider}.env"


class TestIntegrationManager(_IntegrationFixture):

    def test_add_writes_metadata_and_secret_with_0600(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("anthropic", {"api_key": "sk-ant-testkey123"})

        self.assertTrue(self.meta.exists())
        secret_file = self.secret_env_file("anthropic")
        self.assertTrue(secret_file.exists())
        mode = secret_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

        body = secret_file.read_text()
        self.assertIn("api_key=sk-ant-testkey123", body)

    def test_list_returns_sorted_configured_providers(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-xyz_abc"})
        mgr.add("anthropic", {"api_key": "sk-ant-abc_xyz"})

        items = mgr.list()
        self.assertEqual([i["provider"] for i in items], ["anthropic", "openrouter"])
        self.assertEqual(items[0]["method"], "api_key")
        self.assertTrue(items[0]["added_at"])
        self.assertIn("anthropic.env", items[0]["files"][0])

    def test_list_filters_vscode_extension_id_keyed_entries(self):
        """The VS Code extension writes entries under random IDs like
        'integration-1768977015296-3prpe3wd9' into the same shared metadata
        file. Those entries' credentials are not reachable via our MODULES
        lookup, so listing them misleads the user into thinking a provider
        is configured. The list must filter them out.
        """
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-real"})
        # Simulate the extension writing alongside us.
        data = mgr._load_metadata()
        data["integrations"]["integration-1768977015296-3prpe3wd9"] = {
            "provider": "Anthropic",
            "method": "api_key",
            "added_at": "2026-04-20T00:00:00Z",
        }
        data["integrations"]["integration-1775131950811-4a6cyfhtf"] = {
            "provider": "GitHub",
            "method": "pat",
        }
        mgr._save_metadata(data)

        items = mgr.list()
        self.assertEqual([i["provider"] for i in items], ["openrouter"])

    def test_list_normalizes_case_insensitive_provider_keys(self):
        """An 'OpenRouter'-keyed entry (display-cased) should still be
        surfaced, but under its canonical lowercase name so the other
        integration tools (remove, test) can act on it.
        """
        mgr = IntegrationManager(metadata_file=self.meta)
        data = mgr._load_metadata()
        data["integrations"]["OpenRouter"] = {
            "method": "api_key",
            "added_at": "2026-04-24T00:00:00Z",
            "files": [],
        }
        mgr._save_metadata(data)

        items = mgr.list()
        self.assertEqual([i["provider"] for i in items], ["openrouter"])

    def test_list_handles_all_four_provider_casings(self):
        """Each of the four providers under any casing collapses to its
        canonical lowercase form. This matches what different VS Code
        extension builds have shipped over time (GitHub, Github, GITHUB…).
        """
        mgr = IntegrationManager(metadata_file=self.meta)
        data = mgr._load_metadata()
        for key in ("GitHub", "HuggingFace", "Anthropic", "OpenRouter"):
            data["integrations"][key] = {"method": "x", "added_at": "t", "files": []}
        mgr._save_metadata(data)

        items = mgr.list()
        self.assertEqual(
            [i["provider"] for i in items],
            ["anthropic", "github", "huggingface", "openrouter"],
        )

    def test_list_skips_non_dict_values(self):
        """Corrupt metadata (entry value is a string / None / list) must not
        crash list(). Those entries are simply filtered out.
        """
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-good"})
        data = mgr._load_metadata()
        data["integrations"]["garbage_string"] = "not-a-dict"
        data["integrations"]["garbage_none"] = None
        data["integrations"]["garbage_list"] = [1, 2, 3]
        mgr._save_metadata(data)

        items = mgr.list()
        self.assertEqual([i["provider"] for i in items], ["openrouter"])

    def test_list_with_only_foreign_entries_returns_empty(self):
        """Device where ONLY the VS Code extension has written — our list
        is empty, but we must not crash.
        """
        mgr = IntegrationManager(metadata_file=self.meta)
        data = mgr._load_metadata()
        data["integrations"]["integration-1768977015296-3prpe3wd9"] = {
            "method": "api_key",
        }
        mgr._save_metadata(data)

        self.assertEqual(mgr.list(), [])

    def test_env_for_subprocess_ignores_foreign_id_keyed_entries(self):
        """THIS IS THE REAL BUG: credentials written by the VS Code
        extension under random IDs were silently unreachable by
        env_for_subprocess. Tasks thought they were configured but ran
        without the key. Lock in the safe behavior — foreign entries are
        never merged into the subprocess env, regardless of what's in their
        value dicts. (Users are told to re-add via neo_add_integration.)
        """
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-real"})
        data = mgr._load_metadata()
        data["integrations"]["integration-1768977015296-3prpe3wd9"] = {
            "provider": "anthropic",
            "method": "api_key",
            # Even if the entry claims to carry a secret, we refuse to
            # trust it — we can't know which module would load it.
            "api_key": "sk-ant-should-not-leak",
        }
        mgr._save_metadata(data)

        env = mgr.env_for_subprocess()
        self.assertEqual(env.get("OPENROUTER_API_KEY"), "sk-or-real")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_remove_deletes_secret_and_metadata(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("anthropic", {"api_key": "sk-ant-toremove"})
        secret_file = self.secret_env_file("anthropic")
        self.assertTrue(secret_file.exists())

        result = mgr.remove("anthropic")
        self.assertFalse(secret_file.exists())
        self.assertEqual(mgr.list(), [])
        self.assertEqual(result["provider"], "anthropic")
        self.assertTrue(result["removed_files"])

    def test_env_for_subprocess_merges_all_providers(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("anthropic", {"api_key": "sk-ant-A"})
        mgr.add("openrouter", {"api_key": "sk-or-B"})
        mgr.add("huggingface", {"token": "hf_C"})

        env = mgr.env_for_subprocess()
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-A")
        self.assertEqual(env.get("OPENROUTER_API_KEY"), "sk-or-B")
        self.assertEqual(env.get("HF_TOKEN"), "hf_C")
        self.assertEqual(env.get("HUGGING_FACE_HUB_TOKEN"), "hf_C")

    def test_env_ignores_unconfigured_providers(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        self.assertEqual(mgr.env_for_subprocess(), {})

    def test_invalid_credentials_rejected_before_write(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        with self.assertRaises(ValidationError):
            mgr.add("anthropic", {"api_key": "not-sk-ant"})
        self.assertFalse(self.secret_env_file("anthropic").exists())
        self.assertFalse(self.meta.exists())

    def test_unknown_provider_rejected(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        with self.assertRaises(ValidationError):
            mgr.add("snowflake", {"api_key": "whatever"})

    def test_github_round_trip_writes_credentials_file(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("github", {"pat": "ghp_validToken123ABC"})

        self.assertTrue(_prov_github.CREDENTIALS_FILE.exists())
        secret_file = self.secret_env_file("github")
        self.assertTrue(secret_file.exists())
        self.assertIn("@github.com", _prov_github.CREDENTIALS_FILE.read_text())
        self.assertEqual(
            _prov_github.CREDENTIALS_FILE.stat().st_mode & 0o777, 0o600
        )

        env = mgr.env_for_subprocess()
        self.assertEqual(env["GITHUB_TOKEN"], "ghp_validToken123ABC")
        self.assertEqual(env["GH_TOKEN"], "ghp_validToken123ABC")

        mgr.remove("github")
        # Both files gone (github was the only entry in .git-credentials)
        self.assertFalse(_prov_github.CREDENTIALS_FILE.exists())
        self.assertFalse(secret_file.exists())

    def test_github_preserves_other_credentials_on_remove(self):
        # Pre-existing non-github entry in ~/.git-credentials must survive removal.
        _prov_github.CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _prov_github.CREDENTIALS_FILE.write_text(
            "https://user:pat@gitlab.com\n"
        )

        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("github", {"pat": "ghp_Token123"})
        mgr.remove("github")

        self.assertTrue(_prov_github.CREDENTIALS_FILE.exists())
        content = _prov_github.CREDENTIALS_FILE.read_text()
        self.assertIn("@gitlab.com", content)
        self.assertNotIn("@github.com", content)

    def test_huggingface_writes_to_cache_huggingface_token(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("huggingface", {"token": "hf_abcDEF"})
        self.assertTrue(_prov_hf.TOKEN_FILE.exists())
        self.assertEqual(_prov_hf.TOKEN_FILE.read_text(), "hf_abcDEF")
        self.assertEqual(_prov_hf.TOKEN_FILE.stat().st_mode & 0o777, 0o600)

    def test_corrupt_metadata_file_recovered(self):
        self.meta.parent.mkdir(parents=True, exist_ok=True)
        self.meta.write_text("not json at all")
        mgr = IntegrationManager(metadata_file=self.meta)
        # list() must not raise on corrupt metadata
        self.assertEqual(mgr.list(), [])
        # and add still works after recovery
        mgr.add("anthropic", {"api_key": "sk-ant-recovered"})
        self.assertEqual([i["provider"] for i in mgr.list()], ["anthropic"])


class TestListIntegrationsResponse(_IntegrationFixture):
    """Covers the server-level _list_integrations() response shape — the
    ignored-count note that tells the user why their list shrank."""

    def _patched_manager(self):
        from neo_mcp import server as _srv
        return patch.object(_srv, "IntegrationManager", lambda: IntegrationManager(metadata_file=self.meta))

    def test_clean_response_has_no_note(self):
        from neo_mcp.server import _list_integrations
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-clean"})
        with self._patched_manager():
            resp = _list_integrations()
        self.assertEqual(resp["count"], 1)
        self.assertNotIn("note", resp)
        self.assertNotIn("ignored_foreign_entries", resp)

    def test_one_ignored_entry_uses_singular_grammar(self):
        from neo_mcp.server import _list_integrations
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-ok"})
        data = mgr._load_metadata()
        data["integrations"]["integration-1768977015296-3prpe3wd9"] = {"method": "x"}
        mgr._save_metadata(data)
        with self._patched_manager():
            resp = _list_integrations()
        self.assertEqual(resp["ignored_foreign_entries"], 1)
        self.assertIn("1 entry ", resp["note"])
        self.assertNotIn("1 entries", resp["note"])

    def test_multiple_ignored_entries_uses_plural_grammar(self):
        from neo_mcp.server import _list_integrations
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-ok"})
        data = mgr._load_metadata()
        data["integrations"]["integration-a"] = {"method": "x"}
        data["integrations"]["integration-b"] = {"method": "y"}
        data["integrations"]["integration-c"] = {"method": "z"}
        mgr._save_metadata(data)
        with self._patched_manager():
            resp = _list_integrations()
        self.assertEqual(resp["ignored_foreign_entries"], 3)
        self.assertIn("3 entries ", resp["note"])

    def test_response_reproduces_user_reported_scenario(self):
        """End-to-end scenario matching the actual device output the user
        pasted: two integration-<id> rows plus one real OpenRouter row.
        After the fix, the list contains just openrouter and the note
        tells the user exactly why the other two disappeared.
        """
        from neo_mcp.server import _list_integrations
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("openrouter", {"api_key": "sk-or-realkey_abc"})
        data = mgr._load_metadata()
        data["integrations"]["integration-1768977015296-3prpe3wd9"] = {
            "method": "api_key",
        }
        data["integrations"]["integration-1775131950811-4a6cyfhtf"] = {
            "method": "pat",
        }
        mgr._save_metadata(data)

        with self._patched_manager():
            resp = _list_integrations()

        self.assertEqual(resp["count"], 1)
        self.assertEqual(resp["integrations"][0]["provider"], "openrouter")
        self.assertEqual(resp["ignored_foreign_entries"], 2)
        self.assertIn("neo_add_integration", resp["note"])
        self.assertIn("VS Code extension", resp["note"])


class TestSecretStoreSelection(unittest.TestCase):
    """NEO_INTEGRATIONS_BACKEND picks the right backend."""

    def setUp(self):
        self._orig = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)

    def tearDown(self):
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig)

    def test_default_is_file_backend(self):
        self.assertIsInstance(get_secret_store(), FileStore)

    def test_explicit_file_backend(self):
        os.environ["NEO_INTEGRATIONS_BACKEND"] = "file"
        self.assertIsInstance(get_secret_store(), FileStore)

    def test_unknown_value_falls_back_to_file(self):
        os.environ["NEO_INTEGRATIONS_BACKEND"] = "vault"
        self.assertIsInstance(get_secret_store(), FileStore)

    def test_keyring_raises_when_no_functional_backend(self):
        import keyring
        import keyring.backends.fail
        orig = keyring.get_keyring()
        keyring.set_keyring(keyring.backends.fail.Keyring())
        os.environ["NEO_INTEGRATIONS_BACKEND"] = "keyring"
        try:
            with self.assertRaises(RuntimeError):
                get_secret_store()
        finally:
            keyring.set_keyring(orig)


class TestKeyringStoreRoundTrip(unittest.TestCase):
    """End-to-end using an in-memory fake keyring backend."""

    def setUp(self):
        import keyring
        self._orig_backend = keyring.get_keyring()
        self._fake = _make_fake_keyring()
        keyring.set_keyring(self._fake)

        self._orig_env = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)
        os.environ["NEO_INTEGRATIONS_BACKEND"] = "keyring"

        self.td = Path(make_ws())
        self.meta = self.td / "integrations.json"
        self._orig_hf = _prov_hf.TOKEN_FILE
        self._orig_gh = _prov_github.CREDENTIALS_FILE
        _prov_hf.TOKEN_FILE = self.td / "hf_token"
        _prov_github.CREDENTIALS_FILE = self.td / "git-credentials"

    def tearDown(self):
        import keyring
        keyring.set_keyring(self._orig_backend)
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig_env)
        _prov_hf.TOKEN_FILE = self._orig_hf
        _prov_github.CREDENTIALS_FILE = self._orig_gh
        shutil.rmtree(self.td, ignore_errors=True)

    def test_anthropic_secret_goes_into_keyring_not_file(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("anthropic", {"api_key": "sk-ant-inKeyring"})

        # Plaintext .env file was NOT written
        plaintext = Path(self.td) / "integrations" / "anthropic.env"
        self.assertFalse(plaintext.exists())

        # But env injection still works
        env = mgr.env_for_subprocess()
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-inKeyring")

    def test_keyring_entry_removed_on_remove(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("anthropic", {"api_key": "sk-ant-toDelete"})
        self.assertEqual(
            self._fake.get_password("neo-mcp:anthropic", "api_key"),
            "sk-ant-toDelete",
        )
        mgr.remove("anthropic")
        self.assertIsNone(self._fake.get_password("neo-mcp:anthropic", "api_key"))

    def test_huggingface_writes_keyring_and_native_file(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("huggingface", {"token": "hf_dualStore"})
        self.assertEqual(
            self._fake.get_password("neo-mcp:huggingface", "token"),
            "hf_dualStore",
        )
        # Native file still written so huggingface-cli / transformers can read it
        self.assertTrue(_prov_hf.TOKEN_FILE.exists())
        self.assertEqual(_prov_hf.TOKEN_FILE.read_text(), "hf_dualStore")

    def test_github_writes_keyring_and_git_credentials(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("github", {"pat": "ghp_keyringPAT", "username": "alice"})
        self.assertEqual(
            self._fake.get_password("neo-mcp:github", "pat"),
            "ghp_keyringPAT",
        )
        content = _prov_github.CREDENTIALS_FILE.read_text()
        self.assertIn("alice:ghp_keyringPAT@github.com", content)


class TestIntegrationToolsDispatch(unittest.IsolatedAsyncioTestCase):
    """Exercise the MCP tool helper functions in server.py directly.

    Patches the default metadata path and secret_store's INTEGRATIONS_DIR
    so tool handlers (which construct their own IntegrationManager / store
    without args) land in a per-test tmpdir.
    """

    def setUp(self):
        self.td = Path(make_ws())
        self.meta = self.td / "integrations.json"

        self._orig_meta = _int_manager_mod.INTEGRATIONS_METADATA_FILE
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self.meta

        self._orig_int_dir = _secret_store_mod.INTEGRATIONS_DIR
        _secret_store_mod.INTEGRATIONS_DIR = self.td / "integrations"

        self._orig_backend = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)

    def tearDown(self):
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self._orig_meta
        _secret_store_mod.INTEGRATIONS_DIR = self._orig_int_dir
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig_backend)
        shutil.rmtree(self.td, ignore_errors=True)

    def _anthropic_secret_path(self) -> Path:
        return self.td / "integrations" / "anthropic.env"

    def test_list_integrations_empty(self):
        from neo_mcp.server import _list_integrations
        result = _list_integrations()
        self.assertEqual(result, {"count": 0, "integrations": []})

    def test_add_then_list_roundtrip(self):
        from neo_mcp.server import _add_integration, _list_integrations
        _add_integration({"provider": "anthropic", "credentials": {"api_key": "sk-ant-rt"}})
        listing = _list_integrations()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["integrations"][0]["provider"], "anthropic")

    def test_add_missing_provider_raises(self):
        from neo_mcp.server import _add_integration
        with self.assertRaises(ValidationError):
            _add_integration({"credentials": {"api_key": "sk-ant-xxx"}})

    def test_add_rejects_non_dict_credentials(self):
        from neo_mcp.server import _add_integration
        with self.assertRaises(ValidationError):
            _add_integration({"provider": "anthropic", "credentials": "sk-ant-x"})

    def test_remove_returns_removed_files(self):
        from neo_mcp.server import _add_integration, _remove_integration
        _add_integration({"provider": "anthropic", "credentials": {"api_key": "sk-ant-rm"}})
        self.assertTrue(self._anthropic_secret_path().exists())
        result = _remove_integration({"provider": "anthropic"})
        self.assertEqual(result["status"], "removed")
        self.assertTrue(result["removed_files"])
        self.assertFalse(self._anthropic_secret_path().exists())

    async def test_test_integration_reports_ok_on_mocked_probe(self):
        from neo_mcp.server import _add_integration, _test_integration
        _add_integration({"provider": "anthropic", "credentials": {"api_key": "sk-ant-xx"}})

        async def fake_ok() -> tuple[bool, str, int]:
            return True, "ok", 42

        with patch.object(_prov_anthropic, "test_connection", new=fake_ok):
            result = await _test_integration({"provider": "anthropic"})
        self.assertEqual(result, {"provider": "anthropic", "ok": True, "message": "ok", "latency_ms": 42})

    async def test_test_integration_unconfigured_provider(self):
        from neo_mcp.server import _test_integration
        # Provider is known but no credentials stored → tester returns ok=False
        result = await _test_integration({"provider": "anthropic"})
        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["message"])

    def test_add_response_includes_safety_message(self):
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "anthropic",
            "credentials": {"api_key": "sk-ant-safetyTest"},
        })
        self.assertIn("safety", result)
        self.assertIsInstance(result["safety"], str)
        # Must reassure about key location + non-exfiltration
        safety = result["safety"].lower()
        self.assertIn("never leave your machine", safety)
        self.assertIn("never sent to neo's backend", safety)
        self.assertIn("anthropic", safety)

    def test_add_response_instructs_agent_to_relay(self):
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "anthropic",
            "credentials": {"api_key": "sk-ant-relay"},
        })
        self.assertIn("assistant_instruction", result)
        self.assertIn("verbatim", result["assistant_instruction"].lower())

    def test_add_response_reports_backend_and_location(self):
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "anthropic",
            "credentials": {"api_key": "sk-ant-loc"},
        })
        self.assertEqual(result["storage_backend"], "file")
        self.assertTrue(result["storage_location"].endswith("anthropic.env"))
        # The location must be reflected in the safety message
        self.assertIn(result["storage_location"], result["safety"])

    def test_add_response_never_leaks_secret_value(self):
        """The raw credential must never appear anywhere in the tool response."""
        from neo_mcp.server import _add_integration
        import json as _json
        secret = "sk-ant-DO_NOT_LEAK_9f3c1b2a"
        result = _add_integration({
            "provider": "anthropic",
            "credentials": {"api_key": secret},
        })
        # Serialize the whole payload (as MCP would) and scan for the secret
        blob = _json.dumps(result)
        self.assertNotIn(secret, blob)

    def test_add_response_mentions_mode_0600_for_file_backend(self):
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "anthropic",
            "credentials": {"api_key": "sk-ant-mode"},
        })
        self.assertIn("0o600", result["safety"])


class TestProbeRedaction(unittest.IsolatedAsyncioTestCase):
    """_http.probe must never echo a credential-shaped token into its message."""

    async def test_response_body_with_credential_token_is_redacted(self):
        from neo_mcp.integrations.providers._http import probe
        from unittest.mock import AsyncMock, MagicMock, patch

        leaky_body = '{"error":"your key sk-ant-LEAK_ME_123 is invalid"}'
        fake_resp = MagicMock()
        fake_resp.status_code = 400
        fake_resp.text = leaky_body

        class _FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, *a, **kw): return fake_resp

        with patch("neo_mcp.integrations.providers._http.httpx.AsyncClient",
                   return_value=_FakeClient()):
            ok, msg, _ = await probe("GET", "https://x", {})

        self.assertFalse(ok)
        self.assertNotIn("sk-ant-LEAK_ME_123", msg)
        self.assertIn("redacted", msg.lower())

    async def test_clean_response_body_not_redacted(self):
        from neo_mcp.integrations.providers._http import probe
        from unittest.mock import MagicMock, patch

        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = '{"error":"invalid credential"}'

        class _FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, *a, **kw): return fake_resp

        with patch("neo_mcp.integrations.providers._http.httpx.AsyncClient",
                   return_value=_FakeClient()):
            ok, msg, _ = await probe("GET", "https://x", {})

        self.assertFalse(ok)
        self.assertIn("invalid credential", msg)
        self.assertNotIn("redacted", msg.lower())

    async def test_exception_with_credential_token_is_redacted(self):
        from neo_mcp.integrations.providers._http import probe
        from unittest.mock import patch

        class _BoomClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, *a, **kw):
                raise RuntimeError("connection refused from https://user:ghp_LEAK@example.com")

        with patch("neo_mcp.integrations.providers._http.httpx.AsyncClient",
                   return_value=_BoomClient()):
            ok, msg, _ = await probe("GET", "https://x", {})

        self.assertFalse(ok)
        self.assertNotIn("ghp_LEAK", msg)


class TestKeyringPartialWriteRollback(unittest.TestCase):
    """KeyringStore.write must roll back earlier successful fields if a later one errors."""

    def setUp(self):
        import keyring
        self._orig = keyring.get_keyring()
        self._fake = _make_fake_keyring()
        keyring.set_keyring(self._fake)
        self._orig_env = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)
        os.environ["NEO_INTEGRATIONS_BACKEND"] = "keyring"

    def tearDown(self):
        import keyring
        keyring.set_keyring(self._orig)
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig_env)

    def test_second_field_failure_rolls_back_first(self):
        import keyring.errors
        store = KeyringStore()

        # Patch set_password so the second call raises.
        calls = {"n": 0}
        original_set = self._fake.set_password

        def flaky_set(service, username, password):
            calls["n"] += 1
            if calls["n"] == 2:
                raise keyring.errors.PasswordSetError("simulated keyring drop")
            return original_set(service, username, password)

        self._fake.set_password = flaky_set

        with self.assertRaises(keyring.errors.PasswordSetError):
            store.write("github", {"pat": "ghp_ROLLBACK_TEST", "username": "alice"})

        # Restore set_password (tests shouldn't depend on patched state leaking)
        self._fake.set_password = original_set

        # Most important: the first field must have been rolled back.
        self.assertIsNone(
            self._fake.get_password("neo-mcp:github", "pat"),
            "partial write of 'pat' was NOT rolled back after 'username' failed",
        )
        self.assertIsNone(
            self._fake.get_password("neo-mcp:github", "username"),
        )


class TestSafetyMessageDualWriteProviders(unittest.TestCase):
    """Safety message must truthfully name all on-disk locations, not just the
    keyring entry. M-2 from the superpowers review."""

    def setUp(self):
        import keyring
        self._orig = keyring.get_keyring()
        keyring.set_keyring(_make_fake_keyring())
        self._orig_env = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)
        os.environ["NEO_INTEGRATIONS_BACKEND"] = "keyring"

        self.td = Path(make_ws())
        self.meta = self.td / "integrations.json"
        self._orig_meta = _int_manager_mod.INTEGRATIONS_METADATA_FILE
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self.meta

        self._orig_hf = _prov_hf.TOKEN_FILE
        self._orig_gh = _prov_github.CREDENTIALS_FILE
        _prov_hf.TOKEN_FILE = self.td / "hf_token"
        _prov_github.CREDENTIALS_FILE = self.td / "git-credentials"

    def tearDown(self):
        import keyring
        keyring.set_keyring(self._orig)
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig_env)
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self._orig_meta
        _prov_hf.TOKEN_FILE = self._orig_hf
        _prov_github.CREDENTIALS_FILE = self._orig_gh
        shutil.rmtree(self.td, ignore_errors=True)

    def test_huggingface_keyring_mode_safety_mentions_native_file(self):
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "huggingface",
            "credentials": {"token": "hf_DUAL_WRITE_xyz"},
        })
        safety = result["safety"]
        # Must mention BOTH the keyring and the native plaintext file
        self.assertIn("keyring", safety.lower())
        self.assertIn("encrypted at rest", safety.lower())
        self.assertIn(str(_prov_hf.TOKEN_FILE), safety)
        self.assertIn("0o600", safety)

    def test_github_keyring_mode_safety_mentions_git_credentials(self):
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "github",
            "credentials": {"pat": "ghp_DUAL_WRITE_abc"},
        })
        safety = result["safety"]
        self.assertIn("keyring", safety.lower())
        self.assertIn(str(_prov_github.CREDENTIALS_FILE), safety)

    def test_pure_keyring_provider_does_not_claim_file_location(self):
        """Anthropic/openrouter have no native file — safety must not invent one."""
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "anthropic",
            "credentials": {"api_key": "sk-ant-PURE_keyring"},
        })
        safety = result["safety"]
        # Only keyring mentioned, no "second copy" clause
        self.assertIn("keyring", safety.lower())
        self.assertNotIn("second copy", safety.lower())
        self.assertNotIn("/tmp/", safety)  # no stray filesystem paths


class TestAtomicWriteSecret(unittest.TestCase):
    """atomic_write_secret must land at 0o600 with no readable window."""

    def setUp(self):
        self.td = Path(make_ws())

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_final_mode_is_0600(self):
        target = self.td / "secret.env"
        atomic_write_secret(target, "api_key=abc\n")
        self.assertTrue(target.exists())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_text(), "api_key=abc\n")

    def test_replaces_existing_file_atomically(self):
        target = self.td / "secret.env"
        target.write_text("api_key=old\n")
        target.chmod(0o644)  # deliberately wrong starting mode
        atomic_write_secret(target, "api_key=new\n")
        self.assertEqual(target.read_text(), "api_key=new\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_creates_parent_dir(self):
        target = self.td / "deep" / "nested" / "secret.env"
        atomic_write_secret(target, "x=1\n")
        self.assertTrue(target.exists())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_cleans_up_tempfile_on_error(self):
        target = self.td / "secret.env"
        with patch("neo_mcp.integrations._fsutil.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write_secret(target, "leaked=1\n")
        # No tempfile left behind — check dir has zero files
        leftover = list(self.td.iterdir())
        self.assertEqual(leftover, [], f"tempfile leaked: {leftover}")
        self.assertFalse(target.exists())

    def test_no_intermediate_wrong_mode(self):
        """The destination path never exists with a mode other than 0o600.

        We can't race-test the kernel directly but we can confirm the
        implementation never calls write_text directly on the target path
        (which would leak via umask). Instead it uses a tempfile.mkstemp
        which is 0o600 on creation, then os.replace which preserves mode.
        """
        import neo_mcp.integrations._fsutil as fsu
        # The module should NOT import Path.write_text-style primitives as
        # the write path — verify by reading the source.
        source = Path(fsu.__file__).read_text()
        self.assertNotIn(".write_text(", source)
        self.assertIn("os.replace", source)
        self.assertIn("mkstemp", source)


class TestFileLock(unittest.TestCase):
    """file_lock serializes concurrent writers via fcntl.flock."""

    def setUp(self):
        self.td = Path(make_ws())
        self.lock = self.td / "m.lock"

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_lock_releases_after_block(self):
        with file_lock(self.lock):
            pass
        # Can acquire again without blocking
        with file_lock(self.lock):
            pass

    def test_lock_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with file_lock(self.lock):
                raise RuntimeError("boom")
        # Lock is released — acquiring again works
        with file_lock(self.lock):
            pass

    def test_concurrent_threads_serialize(self):
        import threading
        import time
        order: list[str] = []
        barrier = threading.Barrier(2)

        def worker(name: str, hold_ms: int) -> None:
            barrier.wait()
            with file_lock(self.lock):
                order.append(f"{name}-enter")
                time.sleep(hold_ms / 1000.0)
                order.append(f"{name}-exit")

        t1 = threading.Thread(target=worker, args=("A", 80))
        t2 = threading.Thread(target=worker, args=("B", 80))
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Each thread's enter/exit must be adjacent — no interleaving
        self.assertEqual(len(order), 4)
        self.assertEqual(order[0][-5:], "enter")
        self.assertEqual(order[1][-4:], "exit")
        self.assertEqual(order[0][0], order[1][0])  # A-enter/A-exit or B-enter/B-exit
        self.assertEqual(order[2][-5:], "enter")
        self.assertEqual(order[3][-4:], "exit")


class TestConcurrentAddPreservesAllEntries(_IntegrationFixture):
    """Concurrent neo_add_integration calls must not lose entries under the lock."""

    def test_four_providers_added_concurrently_all_survive(self):
        import threading
        mgr = IntegrationManager(metadata_file=self.meta)

        credentials = {
            "anthropic":   {"api_key": "sk-ant-ABCxyz_thread1"},
            "openrouter":  {"api_key": "sk-or-XYZabc_thread2"},
            "github":      {"pat": "ghp_threadThreeTokenABC"},
            "huggingface": {"token": "hf_threadFourTokenXYZ"},
        }

        errors: list[Exception] = []
        def add_worker(provider: str, creds: dict) -> None:
            try:
                mgr.add(provider, creds)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=add_worker, args=(p, c))
            for p, c in credentials.items()
        ]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"concurrent add raised: {errors}")

        # All four entries must be present in metadata
        listing = mgr.list()
        got = sorted(i["provider"] for i in listing)
        self.assertEqual(got, sorted(credentials.keys()))


class TestAddIntegrationSafetyKeyring(unittest.TestCase):
    """Safety message wording changes when the OS keyring backend is in use."""

    def setUp(self):
        import keyring
        self._orig_backend = keyring.get_keyring()
        keyring.set_keyring(_make_fake_keyring())

        self._orig_env = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)
        os.environ["NEO_INTEGRATIONS_BACKEND"] = "keyring"

        self.td = Path(make_ws())
        self.meta = self.td / "integrations.json"
        self._orig_meta = _int_manager_mod.INTEGRATIONS_METADATA_FILE
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self.meta

    def tearDown(self):
        import keyring
        keyring.set_keyring(self._orig_backend)
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig_env)
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self._orig_meta
        shutil.rmtree(self.td, ignore_errors=True)

    def test_safety_message_mentions_keyring_and_encrypted_at_rest(self):
        from neo_mcp.server import _add_integration
        result = _add_integration({
            "provider": "openrouter",
            "credentials": {"api_key": "sk-or-keyring"},
        })
        safety = result["safety"].lower()
        self.assertIn("keyring", safety)
        self.assertIn("encrypted at rest", safety)
        self.assertIn("never leave your machine", safety)
        # Location should reference the keyring service, not a file path
        self.assertTrue(result["storage_location"].startswith("neo-mcp:"))
        self.assertTrue(result["storage_backend"].startswith("keyring"))


class TestRunSubprocessEnvInjection(unittest.IsolatedAsyncioTestCase):
    """run_subprocess must inherit env vars from configured integrations."""

    def setUp(self):
        self.td = Path(make_ws())
        self.meta = self.td / "integrations.json"
        self._orig_meta = _int_manager_mod.INTEGRATIONS_METADATA_FILE
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self.meta

        self._orig_int_dir = _secret_store_mod.INTEGRATIONS_DIR
        _secret_store_mod.INTEGRATIONS_DIR = self.td / "integrations"
        self._orig_backend = os.environ.pop("NEO_INTEGRATIONS_BACKEND", None)

    def tearDown(self):
        _int_manager_mod.INTEGRATIONS_METADATA_FILE = self._orig_meta
        _secret_store_mod.INTEGRATIONS_DIR = self._orig_int_dir
        _restore("NEO_INTEGRATIONS_BACKEND", self._orig_backend)
        shutil.rmtree(self.td, ignore_errors=True)

    async def test_blocking_subprocess_sees_integration_env(self):
        mgr = IntegrationManager(metadata_file=self.meta)
        mgr.add("anthropic", {"api_key": "sk-ant-LEAK_CANARY_9f3c"})

        ws = str(self.td / "ws")
        os.makedirs(ws, exist_ok=True)
        handlers = ActionHandlers(
            job_manager=JobManager(),
            default_workspace=ws,
            thread_workspaces={},
            integrations=mgr,
        )

        result = await handlers.handle_command({
            "action": "run_subprocess",
            "request_id": "env-1",
            "command": 'printf "%s" "$ANTHROPIC_API_KEY"',
            "detach": False,
        })
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["stdout"], "sk-ant-LEAK_CANARY_9f3c")


# ===========================================================================
# PART 30 — init_chat error messages and retry-on-timeout
# ===========================================================================

class TestInitChatErrorMessages(unittest.IsolatedAsyncioTestCase):
    """Fix #2: timeout error string should be actionable and retry once."""

    async def test_timeout_error_string_is_descriptive(self):
        """Empty-message TimeoutException must still produce a useful error."""
        import httpx
        from neo_mcp.backend_client import BackendClient
        from neo_mcp.config import REQUEST_TIMEOUT

        client = BackendClient(auth_token="sk-v1-test")
        # Both attempts raise timeout → retry is exhausted, error surfaces.
        client._http.post = AsyncMock(side_effect=httpx.ReadTimeout(""))
        try:
            await client.init_chat(message="hi", deployment_id="d")
            self.fail("expected RuntimeError")
        except RuntimeError as exc:
            msg = str(exc)
            self.assertIn("timed out", msg)
            self.assertIn(f"{REQUEST_TIMEOUT}s", msg, f"timeout value missing: {msg}")
            self.assertIn("ReadTimeout", msg, f"exception type missing: {msg}")
            # No naked trailing colon.
            self.assertFalse(msg.rstrip().endswith(":"), f"naked colon: {msg!r}")

    async def test_timeout_is_retried_once(self):
        """First-attempt timeout must retry and succeed on attempt 2."""
        import httpx
        from neo_mcp.backend_client import BackendClient

        client = BackendClient(auth_token="sk-v1-test")
        ok = MagicMock()
        ok.status_code = 200
        ok.is_success = True
        ok.content = b'{"thread_id":"t-1"}'
        ok.json = MagicMock(return_value={"thread_id": "t-1"})
        client._http.post = AsyncMock(
            side_effect=[httpx.ReadTimeout(""), ok]
        )
        result = await client.init_chat(message="hi", deployment_id="d")
        self.assertEqual(result["thread_id"], "t-1")
        self.assertEqual(client._http.post.await_count, 2)

    async def test_network_error_string_handles_empty_exception(self):
        import httpx
        from neo_mcp.backend_client import BackendClient

        client = BackendClient(auth_token="sk-v1-test")
        client._http.post = AsyncMock(side_effect=httpx.ConnectError(""))
        try:
            await client.init_chat(message="hi", deployment_id="d")
            self.fail("expected RuntimeError")
        except RuntimeError as exc:
            msg = str(exc)
            self.assertIn("network error", msg)
            self.assertIn("ConnectError", msg)
            self.assertFalse(msg.rstrip().endswith(":"))


# ===========================================================================
# PART 31 — forget_thread — stop_task must evict thread-workspaces entry
# ===========================================================================

class TestForgetThread(unittest.TestCase):
    """Fix #3: after neo_stop_task, the thread-workspaces entry is gone."""

    def setUp(self):
        self.td = make_ws()
        self.p = make_poller_with_mock_send()
        self.p.register_thread_workspace("t1", self.td)
        self.p.register_thread_workspace("t2", self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_forget_thread_removes_from_memory(self):
        self.p.forget_thread("t1")
        self.assertNotIn("t1", self.p._thread_workspaces)
        self.assertIn("t2", self.p._thread_workspaces)

    def test_forget_thread_persists_removal(self):
        import json
        from neo_mcp.paths import THREAD_WORKSPACES_FILE

        self.p.forget_thread("t1")
        raw = json.loads(THREAD_WORKSPACES_FILE.read_text())
        self.assertNotIn("t1", raw)
        self.assertIn("t2", raw)

    def test_forget_unknown_thread_is_noop(self):
        # Must not raise.
        self.p.forget_thread("never-existed")
        self.assertIn("t1", self.p._thread_workspaces)
        self.assertIn("t2", self.p._thread_workspaces)

    def test_forget_thread_also_clears_status_cache(self):
        self.p.set_thread_status("t1", "RUNNING")
        self.p.forget_thread("t1")
        self.assertNotIn("t1", self.p._thread_statuses)

    def test_forget_thread_also_evicts_wrappers(self):
        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.assertIn("t1", self.p._thread_wrappers)
        self.p.forget_thread("t1")
        self.assertNotIn("t1", self.p._thread_wrappers)

    def test_forget_thread_persists_wrapper_removal(self):
        import json
        from neo_mcp.paths import THREAD_WORKSPACES_FILE

        self.p.register_thread_wrapper("t1", "rag_system_langchain_0937")
        self.p.forget_thread("t1")
        raw = json.loads(THREAD_WORKSPACES_FILE.read_text())
        self.assertNotIn("t1", raw)


# ===========================================================================
# PART 32 — Workspace fail-loud (fix B) and submit-register race (fix E)
# ===========================================================================

class TestWorkspaceForFailLoud(unittest.TestCase):
    """Fix B: _workspace_for raises for unknown thread_id instead of silent fallback."""

    def setUp(self):
        self.td = make_ws()
        self.td2 = make_ws()
        self.h, self.ws = make_handlers(
            workspace=self.td,
            thread_workspaces={"known-thread": self.td2},
        )

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)
        shutil.rmtree(self.td2, ignore_errors=True)

    def test_known_thread_returns_registered_workspace(self):
        self.assertEqual(self.h._workspace_for("known-thread"), self.td2)

    def test_unknown_thread_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.h._workspace_for("never-registered")
        msg = str(ctx.exception)
        self.assertIn("never-registered", msg)
        self.assertIn("No workspace registered", msg)
        self.assertIn(self.td, msg, "must name the default ws we refused to fall back to")

    def test_none_thread_id_falls_back_to_default(self):
        """Legacy handshake paths without thread_id still fall through — file
        operations always carry a thread_id, so this is the lower-risk case."""
        self.assertEqual(self.h._workspace_for(None), self.td)


class TestWorkspaceRegistrationRace(unittest.IsolatedAsyncioTestCase):
    """Fix E: handle_command waits briefly for workspace registration."""

    async def asyncSetUp(self):
        self.td = make_ws()
        self.h, self.ws = make_handlers(
            workspace=self.td,
            thread_workspaces={},
        )

    async def asyncTearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    async def test_waits_and_succeeds_when_registration_arrives(self):
        """Dispatch races registration — mapping lands within wait window."""
        async def late_register():
            await asyncio.sleep(0.1)
            self.h._thread_workspaces["racing-thread"] = self.td

        reg_task = asyncio.create_task(late_register())
        result = await self.h.handle_command({
            "action": "write_code",
            "request_id": "race-1",
            "thread_id": "racing-thread",
            "filename": "hello.txt",
            "code": "ok",
        })
        await reg_task
        self.assertEqual(result.get("status"), "success", f"unexpected: {result}")
        self.assertTrue((Path(self.td) / "hello.txt").exists())

    async def test_fails_loud_when_registration_never_arrives(self):
        """If mapping never registers, the handler surfaces a clean error
        rather than silently writing to the default workspace."""
        # Shorten wait so the test doesn't take 500 ms.
        import neo_mcp.action_handlers as ah_mod
        orig = ah_mod._WORKSPACE_REGISTRATION_WAIT_SECONDS
        ah_mod._WORKSPACE_REGISTRATION_WAIT_SECONDS = 0.05
        try:
            result = await self.h.handle_command({
                "action": "write_code",
                "request_id": "fail-1",
                "thread_id": "orphan-thread",
                "filename": "should_not_exist.txt",
                "code": "x",
            })
            self.assertEqual(result.get("status"), "error")
            self.assertIn("No workspace registered", result["error"])
            self.assertIn("orphan-thread", result["error"])
            # Critical: must not have written to default workspace.
            self.assertFalse(
                (Path(self.td) / "should_not_exist.txt").exists(),
                "file leaked into default workspace — fail-loud broken",
            )
        finally:
            ah_mod._WORKSPACE_REGISTRATION_WAIT_SECONDS = orig

    async def test_no_wait_when_already_registered(self):
        """When mapping is already present, handler dispatches immediately."""
        self.h._thread_workspaces["ready-thread"] = self.td
        start = time.monotonic()
        result = await self.h.handle_command({
            "action": "write_code",
            "request_id": "quick-1",
            "thread_id": "ready-thread",
            "filename": "quick.txt",
            "code": "fast",
        })
        elapsed = time.monotonic() - start
        self.assertEqual(result.get("status"), "success")
        self.assertLess(elapsed, 0.05, f"took {elapsed}s — should not wait when already registered")


# ===========================================================================
# PART 33 — Workspace normalization (fix D) and validation (fix C)
# ===========================================================================

class TestWorkspaceNormalization(unittest.TestCase):
    """Fix D: _normalize_workspace produces a single canonical form."""

    def setUp(self):
        self.td = make_ws()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_trailing_slash_is_stripped(self):
        from neo_mcp.server import _normalize_workspace
        self.assertEqual(_normalize_workspace(self.td + "/"), self.td)

    def test_redundant_slashes_collapsed(self):
        from neo_mcp.server import _normalize_workspace
        doubled = self.td.replace("/", "//", 1)
        self.assertEqual(_normalize_workspace(doubled), self.td)

    def test_dot_segments_resolved(self):
        from neo_mcp.server import _normalize_workspace
        with_dot = f"{self.td}/./."
        self.assertEqual(_normalize_workspace(with_dot), self.td)

    def test_symlink_resolved(self):
        from neo_mcp.server import _normalize_workspace
        link = Path(self.td) / "link-to-tmp"
        real = Path(tempfile.mkdtemp())
        try:
            os.symlink(str(real), str(link))
            self.assertEqual(_normalize_workspace(str(link)), str(real.resolve()))
        finally:
            shutil.rmtree(real, ignore_errors=True)

    def test_tilde_expanded(self):
        from neo_mcp.server import _normalize_workspace
        home = str(Path.home().resolve())
        self.assertEqual(_normalize_workspace("~"), home)


class TestWorkspaceValidation(unittest.TestCase):
    """Fix C: _validate_workspace rejects bad paths with clear messages."""

    def setUp(self):
        self.td = make_ws()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_absolute_existing_dir_accepted(self):
        from neo_mcp.server import _validate_workspace
        self.assertIsNone(_validate_workspace(self.td))

    def test_relative_path_rejected(self):
        from neo_mcp.server import _validate_workspace
        err = _validate_workspace("relative/path")
        self.assertIsNotNone(err)
        self.assertIn("absolute", err)

    def test_nonexistent_path_rejected(self):
        from neo_mcp.server import _validate_workspace
        err = _validate_workspace("/tmp/neo-does-not-exist-xyz-12345")
        self.assertIsNotNone(err)
        self.assertIn("does not exist", err)

    def test_file_path_rejected(self):
        from neo_mcp.server import _validate_workspace
        f = Path(self.td) / "file.txt"
        f.write_text("x")
        err = _validate_workspace(str(f))
        self.assertIsNotNone(err)
        self.assertIn("not a directory", err)

    def test_nonwritable_path_rejected(self):
        from neo_mcp.server import _validate_workspace
        import stat
        ro = Path(self.td) / "readonly"
        ro.mkdir()
        # Skip when running as root — os.access ignores DAC for uid 0.
        if os.geteuid() == 0:
            self.skipTest("root bypasses file permissions")
        os.chmod(ro, stat.S_IREAD | stat.S_IEXEC)
        try:
            err = _validate_workspace(str(ro))
            self.assertIsNotNone(err)
            self.assertIn("not writable", err)
        finally:
            os.chmod(ro, stat.S_IRWXU)

    def test_submit_rejects_nonexistent_workspace(self):
        """End-to-end: _submit_task returns error before calling init_chat."""
        from neo_mcp.server import _submit_task
        client = MagicMock()
        client.init_chat = AsyncMock()
        poller = MagicMock()
        result = asyncio.run(_submit_task(
            client, "dep-id", poller, self.td,
            {"message": "hi", "workspace": "/tmp/neo-absolutely-not-there-98765"},
        ))
        self.assertIn("error", result)
        self.assertIn("does not exist", result["error"])
        client.init_chat.assert_not_called()

    def test_submit_normalizes_trailing_slash_before_mcp_self_check(self):
        """Trailing-slash variants must resolve to the same form that
        downstream code (register_thread_workspace) will use."""
        from neo_mcp.server import _submit_task
        client = MagicMock()
        client.init_chat = AsyncMock(return_value={"thread_id": "t-norm"})
        poller = MagicMock()
        asyncio.run(_submit_task(
            client, "dep-id", poller, self.td,
            {"message": "hi", "workspace": self.td + "/"},
        ))
        # init_chat was called with the normalized (no trailing slash) form.
        call = client.init_chat.await_args
        self.assertEqual(call.kwargs["workspace"], self.td)
        # Register was called with the same canonical path.
        poller.register_thread_workspace.assert_called_once_with("t-norm", self.td)


# ===========================================================================
# PART 34 — Remap regex: substring false positives (fix F)
# ===========================================================================

class TestRemapCommandPathsRegex(unittest.TestCase):
    """Fix F: /workspace, /app, /project roots must not match as substrings
    inside longer filenames. Regression: concurrent tasks where Neo assigned
    a wrapper folder starting with 'workspace_' caused catastrophic rewrites."""

    def setUp(self):
        self.td = make_ws()
        self.h, _ = make_handlers(
            workspace=self.td,
            thread_workspaces={"t": self.td},
        )
        self.ws = Path(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    # --- Legit container-root usage: MUST remap ---

    def test_remap_bare_app_root(self):
        out = self.h._remap_command_paths("ls /app", self.ws)
        self.assertEqual(out, f"ls {self.td}")

    def test_remap_app_with_file(self):
        out = self.h._remap_command_paths("echo x > /app/file.txt", self.ws)
        self.assertEqual(out, f"echo x > {self.td}/file.txt")

    def test_remap_app_with_wrapper_strips_wrapper(self):
        out = self.h._remap_command_paths(
            "echo x > /app/wrapper_0635/file.txt", self.ws,
        )
        # wrapper stripped because len(parts) >= 2
        self.assertEqual(out, f"echo x > {self.td}/file.txt")

    def test_remap_quoted_path(self):
        out = self.h._remap_command_paths('cat "/app/foo.txt"', self.ws)
        self.assertEqual(out, f'cat "{self.td}/foo.txt"')

    def test_remap_workspace_root(self):
        out = self.h._remap_command_paths("cat /workspace/a.txt", self.ws)
        self.assertEqual(out, f"cat {self.td}/a.txt")

    def test_remap_app_project_root(self):
        out = self.h._remap_command_paths("ls /app/project/foo", self.ws)
        self.assertEqual(out, f"ls {self.td}/foo")

    # --- Substring false positives: MUST NOT remap ---

    def test_no_match_app_dash_suffix(self):
        """/app-backup is a real path name, not a container-root prefix."""
        cmd = "ls /app-backup/data"
        self.assertEqual(self.h._remap_command_paths(cmd, self.ws), cmd)

    def test_no_match_app_underscore_suffix(self):
        cmd = "ls /app_extra/file"
        self.assertEqual(self.h._remap_command_paths(cmd, self.ws), cmd)

    def test_no_match_workspace_underscore_suffix(self):
        """The exact concurrent-bug trigger: /workspace_marker_setup_XXXX
        was matching /workspace inside, corrupting the path."""
        cmd = "cat /workspace_marker_setup_0635/marker_B.txt"
        self.assertEqual(self.h._remap_command_paths(cmd, self.ws), cmd)

    def test_no_match_workspace_dash_suffix(self):
        cmd = "cat /workspace-fake/x.txt"
        self.assertEqual(self.h._remap_command_paths(cmd, self.ws), cmd)

    def test_no_match_applepie(self):
        """Longer word starting with /app: must not steal the /app prefix."""
        cmd = "echo /applepie"
        self.assertEqual(self.h._remap_command_paths(cmd, self.ws), cmd)

    def test_no_double_remap_when_workspace_and_app_both_match_pre_fix(self):
        """The exact catastrophic pattern from the concurrent-task live run:
        BEFORE the fix, /workspace inside /app/workspace_... got rewritten to
        the workspace path, which then made /app match the now-broken string.
        After the fix, only /app matches as intended."""
        cmd = "echo -n workspace-B > /app/workspace_marker_setup_0635/marker_B.txt"
        expected = f"echo -n workspace-B > {self.td}/marker_B.txt"
        self.assertEqual(self.h._remap_command_paths(cmd, self.ws), expected)


# ===========================================================================
# PART 35 — Relative-path rejection order (fix G)
# ===========================================================================

class TestRelativePathRejectionOrder(unittest.IsolatedAsyncioTestCase):
    """Fix G: relative-path error must fire BEFORE normalization prepends cwd."""

    async def test_submit_rejects_relative_with_clear_error(self):
        from neo_mcp.server import _submit_task
        client = MagicMock()
        client.init_chat = AsyncMock()
        poller = MagicMock()
        td = make_ws()
        try:
            result = await _submit_task(
                client, "dep-id", poller, td,
                {"message": "hi", "workspace": "relative/path"},
            )
            self.assertIn("error", result)
            # The error must mention the RAW input, not some resolved form.
            self.assertIn("'relative/path'", result["error"])
            self.assertIn("absolute", result["error"])
            # Must NOT have called init_chat.
            client.init_chat.assert_not_called()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    async def test_submit_accepts_tilde_path(self):
        from neo_mcp.server import _submit_task
        client = MagicMock()
        client.init_chat = AsyncMock(return_value={"thread_id": "t-tilde"})
        poller = MagicMock()
        td = make_ws()
        try:
            # Use the tmp workspace aliased via a tilde path to home + relative.
            # We can't actually create a file under ~ in a test, so instead we
            # just verify that a tilde input doesn't trip the relative-path
            # check (it falls through to the does-not-exist error).
            result = await _submit_task(
                client, "dep-id", poller, td,
                {"message": "hi", "workspace": "~/this-dir-does-not-exist"},
            )
            self.assertIn("error", result)
            # It should say "does not exist", NOT "must be absolute" — proving
            # the tilde path got through the absolute-check.
            self.assertIn("does not exist", result["error"])
            self.assertNotIn("absolute", result["error"])
        finally:
            shutil.rmtree(td, ignore_errors=True)

    async def test_submit_accepts_absolute_path(self):
        from neo_mcp.server import _submit_task
        client = MagicMock()
        client.init_chat = AsyncMock(return_value={"thread_id": "t-abs"})
        poller = MagicMock()
        td = make_ws()
        try:
            result = await _submit_task(
                client, "dep-id", poller, td,
                {"message": "hi", "workspace": td},
            )
            self.assertEqual(result.get("thread_id"), "t-abs")
            client.init_chat.assert_awaited_once()
        finally:
            shutil.rmtree(td, ignore_errors=True)


class TestPollerDetectionPidReuse(unittest.TestCase):
    """A dead daemon's PID may be reused by an unrelated process (bash, node).
    Guard against misidentifying the reused PID as a live neo daemon — that
    would suppress the in-process Python poller and cause init_chat hangs.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patches = []
        from neo_mcp import server as _srv
        # Redirect DAEMON_DIR / LOCK_FILE into a sandboxed tmp directory.
        self._daemon_dir = Path(self._tmp) / "daemon"
        self._daemon_dir.mkdir(parents=True, exist_ok=True)
        self._patches.append(patch.object(_srv, "DAEMON_DIR", self._daemon_dir))
        self._patches.append(patch.object(_srv, "LOCK_FILE", self._daemon_dir / "neo-mcp.lock"))
        for p in self._patches:
            p.start()
        self._srv = _srv

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_false_when_pid_is_not_a_neo_daemon(self):
        # Use our own PID but mask os.getpid() so the suite doesn't short-circuit.
        real_pid = os.getpid()
        pid_file = self._daemon_dir / "daemon_12345678.pid"
        pid_file.write_text(str(real_pid))
        with patch.object(self._srv.os, "getpid", return_value=real_pid + 1), \
             patch.object(self._srv, "_pid_cmdline", return_value="/bin/bash"):
            self.assertFalse(self._srv._poller_already_running("12345678-aaaa-bbbb-cccc-ddddeeeeffff"))
        # Stale file must be cleaned up so next startup doesn't re-trip.
        self.assertFalse(pid_file.exists())

    def test_returns_true_when_cmdline_matches_neo_mcp(self):
        real_pid = os.getpid()
        pid_file = self._daemon_dir / "daemon_12345678.pid"
        pid_file.write_text(str(real_pid))
        with patch.object(self._srv.os, "getpid", return_value=real_pid + 1), \
             patch.object(self._srv, "_pid_cmdline", return_value="node /usr/bin/neo-mcp-daemon /ws"):
            self.assertTrue(self._srv._poller_already_running("12345678-aaaa-bbbb-cccc-ddddeeeeffff"))
        # Live neo daemon's PID file must NOT be deleted.
        self.assertTrue(pid_file.exists())

    def test_falls_back_to_liveness_when_cmdline_unknown(self):
        real_pid = os.getpid()
        pid_file = self._daemon_dir / "daemon_12345678.pid"
        pid_file.write_text(str(real_pid))
        with patch.object(self._srv.os, "getpid", return_value=real_pid + 1), \
             patch.object(self._srv, "_pid_cmdline", return_value=None):
            # cmdline unreadable → fall back to alive-only check → treat as live daemon.
            self.assertTrue(self._srv._poller_already_running("12345678-aaaa-bbbb-cccc-ddddeeeeffff"))

    def test_dead_pid_file_is_removed(self):
        # Pick a PID that's almost certainly not alive.
        pid_file = self._daemon_dir / "daemon_12345678.pid"
        pid_file.write_text("9999999")
        self.assertFalse(self._srv._poller_already_running("12345678-aaaa-bbbb-cccc-ddddeeeeffff"))
        self.assertFalse(pid_file.exists())


if __name__ == "__main__":
    unittest.main()
