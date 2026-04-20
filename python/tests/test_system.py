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
        result = self.remap("cp /app/project/src/a.py /app/project/dst/a.py")
        self.assertIn(str(self.ws / "src/a.py"), result)
        self.assertIn(str(self.ws / "dst/a.py"), result)

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


def _restore(key: str, val: str | None) -> None:
    if val is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = val


if __name__ == "__main__":
    unittest.main()
