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
        # /app/project/{project-name}/{file}: first segment is the project wrapper and is
        # stripped. "src" here is the project name on the backend; file lands at workspace root.
        r = arun(self.h.handle_command(self.cmd(filename="/app/project/src/main.py", code="# main")))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "main.py").exists())

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
        # Must be treated as /app/project/myproj/model.py and remapped to workspace root.
        r = arun(self.h.handle_command(
            self.cmd(filename="app/project/myproj/model.py", code="# m")
        ))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.td, "model.py").exists(),
                        "container-relative filename must land at workspace root")
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
        """When workspace name matches the wrapper, stripping still gives the correct path."""
        ws_name = self.ws.name
        result = self.remap_strip(f"/app/project/{ws_name}/model.py")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_strip_wrapper_mismatched_name(self):
        """Core fix: project wrapper is stripped even when workspace name differs."""
        result = self.remap_strip("/app/project/test_2/model.py")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_strip_wrapper_nested_path(self):
        """Deep path: wrapper stripped, subdirectory structure preserved."""
        result = self.remap_strip("/app/project/test_2/src/utils.py")
        self.assertEqual(result, str(self.ws / "src/utils.py"))

    def test_strip_wrapper_filename_at_container_root_kept(self):
        """Single-segment after /app/project/ is treated as a filename, not stripped."""
        result = self.remap_strip("/app/project/model.py")
        self.assertEqual(result, str(self.ws / "model.py"))

    def test_strip_wrapper_workdir_single_segment_maps_to_workspace(self):
        """workdir=/app/project/test_2 (is_workdir=True) maps to workspace root."""
        result = self.remap_strip_wd("/app/project/test_2")
        self.assertEqual(result, str(self.ws))

    def test_strip_wrapper_workdir_subdir(self):
        """workdir=/app/project/test_2/demo → workspace/demo."""
        result = self.remap_strip_wd("/app/project/test_2/demo")
        self.assertEqual(result, str(self.ws / "demo"))

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

    def test_unknown_thread_falls_back_to_default_workspace(self):
        h = ActionHandlers(JobManager(), self.workspaces[0], {})
        r = arun(h.handle_command({
            "action": "write_code", "request_id": "r", "thread_id": "unknown",
            "filename": "fallback.py", "code": "# fallback",
        }))
        self.assertEqual(r["status"], "success")
        self.assertTrue(Path(self.workspaces[0], "fallback.py").exists())


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
# PART 16 — Deployment ID
# ===========================================================================

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
        self.h, _ = make_handlers(workspace=self.td)
        self.ws = Path(self.td)
        self.tid = "t-wrap-1"

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_extract_wrapper_from_app_path(self):
        self.assertEqual(
            self.h._extract_wrapper(Path("/app/movie_recommender_system_1703/data/x.txt")),
            "movie_recommender_system_1703",
        )
        self.assertEqual(
            self.h._extract_wrapper(Path("/app/project/foo/bar.py")),
            "foo",
        )

    def test_extract_wrapper_returns_none_for_non_container_paths(self):
        self.assertIsNone(self.h._extract_wrapper(Path("/tmp/script.sh")))
        self.assertIsNone(self.h._extract_wrapper(Path("/app/bare.py")))  # no wrapper after /app/

    def test_record_wrapper_first_abs_write_captures_slug(self):
        self.h._record_wrapper(self.tid, Path("/app/my_proj_0001/data/a.txt"))
        self.assertEqual(self.h._thread_wrappers[self.tid], "my_proj_0001")

    def test_record_wrapper_is_sticky_not_overwritten(self):
        self.h._record_wrapper(self.tid, Path("/app/my_proj_0001/data/a.txt"))
        self.h._record_wrapper(self.tid, Path("/app/different_proj_9999/data/b.txt"))
        self.assertEqual(self.h._thread_wrappers[self.tid], "my_proj_0001")

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
        self.h._thread_wrappers[self.tid] = "movie_recommender_system_1703"
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

    def test_strip_absolute_path_all_container_roots(self):
        ws = Path("/tmp/host_ws")
        for root in ("/app/project", "/app", "/workspace", "/project"):
            text = f"cat {root}/my_proj_0001/data.txt"
            result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
            self.assertEqual(
                result, "cat /tmp/host_ws/data.txt",
                f"failed for container root {root}",
            )

    def test_strip_absolute_does_not_match_similar_name(self):
        # /app/my_proj_0001_backup must NOT be rewritten (different name).
        ws = Path("/tmp/host_ws")
        text = "cat /app/my_proj_0001_backup/x.txt"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
        self.assertEqual(result, "cat /app/my_proj_0001_backup/x.txt")

    def test_strip_absolute_longest_root_wins(self):
        # /app/project should match before /app, so no double-remap.
        ws = Path("/tmp/host_ws")
        text = "ls /app/project/my_proj_0001/foo"
        result = self.h._strip_wrapper_prefixes(text, "my_proj_0001", ws)
        self.assertEqual(result, "ls /tmp/host_ws/foo")

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
        self.h._thread_wrappers[self.tid] = "my_proj_0001"
        result = self.h._apply_wrapper_rewrite(
            "target = '/app/my_proj_0001'", self.tid,
        )
        # _apply_wrapper_rewrite resolves the workspace, so symlink-free paths on
        # Linux are unchanged but /tmp → /private/tmp on macOS.
        ws_resolved = self.ws.resolve()
        self.assertEqual(result, f"target = '{ws_resolved}'")


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


if __name__ == "__main__":
    unittest.main()
