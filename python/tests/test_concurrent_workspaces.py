"""Test: concurrent tasks each write to their own workspace folder.

Scenario: 3 tasks submitted simultaneously to test/test1, test/test2, test/test3.
Verifies that write_code commands are routed to the correct folder per thread_id
and that _remap_to_workspace does not cross-contaminate paths.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from neo_mcp.action_handlers import ActionHandlers
from neo_mcp.job_manager import JobManager


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestConcurrentWorkspaceIsolation(unittest.TestCase):
    """ActionHandlers correctly routes write_code to per-thread workspaces."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.ws1 = os.path.join(self._tmpdir, "test1")
        self.ws2 = os.path.join(self._tmpdir, "test2")
        self.ws3 = os.path.join(self._tmpdir, "test3")
        for d in (self.ws1, self.ws2, self.ws3):
            os.makedirs(d, exist_ok=True)

        self.thread_workspaces = {
            "thread-1": self.ws1,
            "thread-2": self.ws2,
            "thread-3": self.ws3,
        }
        self.handlers = ActionHandlers(
            job_manager=JobManager(),
            default_workspace=self._tmpdir,
            thread_workspaces=self.thread_workspaces,
        )

    # ------------------------------------------------------------------
    # write_code with relative filename
    # ------------------------------------------------------------------

    def test_relative_filename_routes_to_correct_workspace(self):
        for tid, ws, name in [
            ("thread-1", self.ws1, "model_t1.py"),
            ("thread-2", self.ws2, "model_t2.py"),
            ("thread-3", self.ws3, "model_t3.py"),
        ]:
            result = run(self.handlers.handle_command({
                "action": "write_code",
                "request_id": f"req-{tid}",
                "thread_id": tid,
                "filename": name,
                "code": f"# {tid}",
            }))
            self.assertEqual(result["status"], "success", f"thread {tid} write failed: {result}")
            written = Path(result["data"]["file_path"])
            self.assertTrue(written.exists(), f"File not created: {written}")
            self.assertTrue(str(written).startswith(ws), f"File {written} not inside workspace {ws}")

        # No cross-contamination: each workspace has exactly its own file
        self.assertEqual(set(os.listdir(self.ws1)), {"model_t1.py"})
        self.assertEqual(set(os.listdir(self.ws2)), {"model_t2.py"})
        self.assertEqual(set(os.listdir(self.ws3)), {"model_t3.py"})

    # ------------------------------------------------------------------
    # write_code with absolute backend container path
    # ------------------------------------------------------------------

    def test_absolute_container_path_remapped_per_thread(self):
        """Neo sends /app/project/src/train.py — must land in each thread's workspace."""
        for tid, ws in [("thread-1", self.ws1), ("thread-2", self.ws2), ("thread-3", self.ws3)]:
            result = run(self.handlers.handle_command({
                "action": "write_code",
                "request_id": f"req-abs-{tid}",
                "thread_id": tid,
                "filename": "/app/project/src/train.py",
                "code": f"# train for {tid}",
            }))
            self.assertEqual(result["status"], "success", f"thread {tid}: {result}")
            written = Path(result["data"]["file_path"])
            self.assertTrue(str(written).startswith(ws),
                            f"File {written} not inside workspace {ws}")
            self.assertTrue(written.exists())

        # Verify different workspaces received different files
        train1 = Path(self.ws1, "src", "train.py")
        train2 = Path(self.ws2, "src", "train.py")
        train3 = Path(self.ws3, "src", "train.py")
        self.assertTrue(train1.exists(), f"Missing {train1}")
        self.assertTrue(train2.exists(), f"Missing {train2}")
        self.assertTrue(train3.exists(), f"Missing {train3}")

        self.assertEqual(train1.read_text(), "# train for thread-1")
        self.assertEqual(train2.read_text(), "# train for thread-2")
        self.assertEqual(train3.read_text(), "# train for thread-3")

    # ------------------------------------------------------------------
    # write_code with absolute workdir (relative filename)
    # ------------------------------------------------------------------

    def test_absolute_workdir_remapped_per_thread(self):
        """Neo sends filename='run.sh', workdir='/app/project/scripts' — remap workdir per thread."""
        for tid, ws in [("thread-1", self.ws1), ("thread-2", self.ws2), ("thread-3", self.ws3)]:
            result = run(self.handlers.handle_command({
                "action": "write_code",
                "request_id": f"req-wd-{tid}",
                "thread_id": tid,
                "filename": "run.sh",
                "workdir": "/app/project/scripts",
                "code": f"#!/bin/bash\necho {tid}",
            }))
            self.assertEqual(result["status"], "success", f"thread {tid}: {result}")
            written = Path(result["data"]["file_path"])
            self.assertTrue(str(written).startswith(ws),
                            f"File {written} not inside workspace {ws}")
            self.assertTrue(written.exists())

    # ------------------------------------------------------------------
    # Default workspace fallback (no thread_id)
    # ------------------------------------------------------------------

    def test_no_thread_id_uses_default_workspace(self):
        result = run(self.handlers.handle_command({
            "action": "write_code",
            "request_id": "req-nothread",
            "filename": "default.py",
            "code": "# default",
        }))
        self.assertEqual(result["status"], "success")
        written = Path(result["data"]["file_path"])
        self.assertTrue(str(written).startswith(self._tmpdir))

    # ------------------------------------------------------------------
    # Concurrent coroutines — all 3 tasks run at the same time
    # ------------------------------------------------------------------

    def test_concurrent_writes_all_land_correctly(self):
        """Simulate 3 tasks running concurrently (interleaved asyncio coroutines)."""
        async def _run_concurrent():
            tasks = [
                self.handlers.handle_command({
                    "action": "write_code",
                    "request_id": f"req-concurrent-{i}",
                    "thread_id": f"thread-{i}",
                    "filename": "/app/project/output.py",
                    "code": f"# task {i}",
                })
                for i in range(1, 4)
            ]
            return await asyncio.gather(*tasks)

        results = asyncio.get_event_loop().run_until_complete(_run_concurrent())
        for i, result in enumerate(results, 1):
            self.assertEqual(result["status"], "success", f"task {i}: {result}")

        out1 = Path(self.ws1, "output.py")
        out2 = Path(self.ws2, "output.py")
        out3 = Path(self.ws3, "output.py")
        self.assertEqual(out1.read_text(), "# task 1")
        self.assertEqual(out2.read_text(), "# task 2")
        self.assertEqual(out3.read_text(), "# task 3")


class TestRemapToWorkspace(unittest.TestCase):
    """Unit tests for _remap_to_workspace path logic."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.handlers = ActionHandlers(
            job_manager=JobManager(),
            default_workspace=self._tmpdir,
            thread_workspaces={},
        )

    def _remap(self, path_str: str, workspace: str, workdir: str | None = None) -> Path:
        return self.handlers._remap_to_workspace(
            Path(path_str).resolve(), Path(workspace), workdir
        )

    def test_standard_app_project_path(self):
        ws = "/home/user/myproject"
        result = self._remap("/app/project/src/model.py", ws)
        self.assertEqual(result, Path(ws) / "src" / "model.py")

    def test_dedup_workspace_name_in_path(self):
        """workspace=.../test_2, Neo sends /app/project/test_2/model.py → no double nesting."""
        ws = "/home/user/test_2"
        result = self._remap("/app/project/test_2/model.py", ws)
        # Should be .../test_2/model.py, NOT .../test_2/test_2/model.py
        self.assertEqual(result, Path(ws) / "model.py")

    def test_no_dedup_when_name_differs(self):
        """workspace=.../myproject, path has test_2 as first segment — keep it."""
        ws = "/home/user/myproject"
        result = self._remap("/app/project/test_2/model.py", ws)
        self.assertEqual(result, Path(ws) / "test_2" / "model.py")

    def test_app_root_path(self):
        ws = "/home/user/project"
        result = self._remap("/app/main.py", ws)
        self.assertEqual(result, Path(ws) / "main.py")

    def test_workspace_root_path(self):
        ws = "/home/user/project"
        result = self._remap("/app/project/", ws)
        self.assertEqual(str(result), ws)

    def test_unknown_root_uses_filename(self):
        ws = "/home/user/project"
        result = self._remap("/some/other/deep/file.txt", ws)
        self.assertEqual(result, Path(ws) / "file.txt")


class TestGetFileSecurity(unittest.TestCase):
    """_get_file must not read files outside workspace/tmp."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.ws = os.path.join(self._tmpdir, "workspace")
        os.makedirs(self.ws)
        # Create a legitimate file inside workspace
        self.legit = os.path.join(self.ws, "legit.py")
        Path(self.legit).write_text("# ok")
        self.thread_workspaces = {"t1": self.ws}
        self.handlers = ActionHandlers(
            job_manager=JobManager(),
            default_workspace=self.ws,
            thread_workspaces=self.thread_workspaces,
        )

    def test_read_file_inside_workspace(self):
        result = run(self.handlers.handle_command({
            "action": "get_file",
            "request_id": "r1",
            "thread_id": "t1",
            "file_path": "legit.py",
        }))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["file_content"], "# ok")

    def test_absolute_container_path_remapped_not_read_directly(self):
        """Neo sends /app/project/legit.py — must remap to workspace, not read /app/project."""
        # The remapped path doesn't exist on this machine, so we expect "not found"
        # rather than a security bypass to an arbitrary absolute path.
        result = run(self.handlers.handle_command({
            "action": "get_file",
            "request_id": "r2",
            "thread_id": "t1",
            "file_path": "/app/project/legit.py",
        }))
        # Either not found (path doesn't exist locally) or success (was remapped to workspace)
        # Either way it must NOT have read an unexpected file from outside workspace
        if result["status"] == "success":
            self.assertTrue(
                result["data"]["file_path"].startswith(self.ws),
                f"file_path {result['data']['file_path']} escaped workspace {self.ws}",
            )

    def test_arbitrary_absolute_path_blocked(self):
        """Absolute path outside workspace and /tmp must not be readable."""
        # Create a real file outside workspace to make it a valid read target
        outside = os.path.join(self._tmpdir, "outside_secret.txt")
        Path(outside).write_text("secret")
        result = run(self.handlers.handle_command({
            "action": "get_file",
            "request_id": "r3",
            "thread_id": "t1",
            "file_path": outside,
        }))
        # Must NOT succeed with file content pointing outside workspace
        if result["status"] == "success":
            read_path = result["data"]["file_path"]
            self.assertTrue(
                read_path.startswith(self.ws) or read_path.startswith("/tmp"),
                f"Security: read file outside allowed paths: {read_path}",
            )


class TestJobCleanup(unittest.TestCase):
    """cleanup_old_jobs removes stale entries from memory."""

    def test_cleanup_removes_old_completed_jobs(self):
        jm = JobManager()
        # Manually insert a fake old job
        from datetime import timedelta
        from neo_mcp.job_manager import _Job
        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        old_job = _Job(
            job_id="old-job",
            pid=None,
            command="echo old",
            working_directory="/tmp",
            thread_id="t1",
            started_at=old_time,
            completed_at=old_time,
            exit_code=0,
        )
        jm._jobs["old-job"] = old_job
        self.assertIn("old-job", jm._jobs)
        jm.cleanup_old_jobs()
        self.assertNotIn("old-job", jm._jobs)

    def test_cleanup_keeps_recent_jobs(self):
        jm = JobManager()
        from neo_mcp.job_manager import _Job
        recent_job = _Job(
            job_id="recent-job",
            pid=None,
            command="echo recent",
            working_directory="/tmp",
            thread_id="t2",
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            exit_code=0,
        )
        jm._jobs["recent-job"] = recent_job
        jm.cleanup_old_jobs()
        self.assertIn("recent-job", jm._jobs)

    def test_cleanup_keeps_running_old_jobs(self):
        """Jobs still running (exit_code=None) should NOT be evicted regardless of age."""
        jm = JobManager()
        from datetime import timedelta
        from neo_mcp.job_manager import _Job
        old_running = _Job(
            job_id="old-running",
            pid=None,
            command="sleep 9999",
            working_directory="/tmp",
            thread_id="t3",
            started_at=datetime.now(timezone.utc) - timedelta(hours=25),
            completed_at=None,
            exit_code=None,
        )
        jm._jobs["old-running"] = old_running
        jm.cleanup_old_jobs()
        self.assertIn("old-running", jm._jobs)


class TestWorkspaceRegistration(unittest.TestCase):
    """BackendPoller.register_thread_workspace feeds ActionHandlers correctly."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.thread_workspaces: dict[str, str] = {}
        self.handlers = ActionHandlers(
            job_manager=JobManager(),
            default_workspace=self._tmpdir,
            thread_workspaces=self.thread_workspaces,
        )

    def test_workspace_for_unknown_thread_returns_default(self):
        result = self.handlers._workspace_for("unknown-thread")
        self.assertEqual(result, self._tmpdir)

    def test_workspace_for_registered_thread(self):
        ws = os.path.join(self._tmpdir, "sub")
        os.makedirs(ws)
        self.thread_workspaces["t-abc"] = ws
        self.assertEqual(self.handlers._workspace_for("t-abc"), ws)

    def test_register_isolates_multiple_threads(self):
        ws_a = os.path.join(self._tmpdir, "a")
        ws_b = os.path.join(self._tmpdir, "b")
        self.thread_workspaces["thread-a"] = ws_a
        self.thread_workspaces["thread-b"] = ws_b
        self.assertEqual(self.handlers._workspace_for("thread-a"), ws_a)
        self.assertEqual(self.handlers._workspace_for("thread-b"), ws_b)
        self.assertNotEqual(
            self.handlers._workspace_for("thread-a"),
            self.handlers._workspace_for("thread-b"),
        )


if __name__ == "__main__":
    unittest.main()
