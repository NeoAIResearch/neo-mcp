"""Tests for neo_mcp — no network calls required.

Coverage:
  TestWriteCode               — relative path, absolute container path, absolute workdir,
                                subdirectory auto-creation, path traversal blocked,
                                missing fields, unicode content, overwrite
  TestGetFileSecurity         — reads inside workspace, container path remapped,
                                absolute path outside workspace blocked,
                                relative path traversal blocked, missing file,
                                missing file_path field
  TestRemapToWorkspace        — all known container roots, deduplication, exact root,
                                workdir hint, unknown root fallback
  TestListFiles               — basic listing, hidden files, max_depth, missing dir
  TestCreateSession           — with/without session_id
  TestUnknownAction           — unknown action returns clean error
  TestConcurrentWorkspaceIsolation — 3 threads × 3 workspaces, no cross-contamination,
                                asyncio.gather concurrent writes
  TestJobCleanup              — evicts old completed, keeps recent, keeps running
  TestWorkspaceRegistration   — default fallback, per-thread lookup, isolation
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from neo_mcp.action_handlers import ActionHandlers
from neo_mcp.job_manager import JobManager, _Job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def arun(coro):
    """Run a single coroutine cleanly (no event-loop deprecation warning)."""
    return asyncio.run(coro)


def make_handlers(
    tmpdir: str,
    workspace: str | None = None,
    thread_workspaces: dict[str, str] | None = None,
) -> ActionHandlers:
    ws = workspace or tmpdir
    tw = {} if thread_workspaces is None else thread_workspaces
    return ActionHandlers(
        job_manager=JobManager(),
        default_workspace=ws,
        thread_workspaces=tw,
    )


def _fake_job(
    job_id: str = "j1",
    *,
    thread_id: str = "t1",
    exit_code: int | None = 0,
    hours_old: float = 0.0,
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


# ---------------------------------------------------------------------------
# TestWriteCode
# ---------------------------------------------------------------------------

class TestWriteCode(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.ws = os.path.join(self._td, "workspace")
        os.makedirs(self.ws)
        self.h = make_handlers(self._td, workspace=self.ws,
                               thread_workspaces={"t1": self.ws})

    def _write(self, **kwargs):
        return arun(self.h.handle_command({"action": "write_code", "request_id": "r",
                                           "thread_id": "t1", **kwargs}))

    # --- happy path ---

    def test_relative_filename_lands_in_workspace(self):
        res = self._write(filename="model.py", code="# hi")
        self.assertEqual(res["status"], "success")
        self.assertEqual(Path(res["data"]["file_path"]).read_text(), "# hi")
        self.assertTrue(res["data"]["file_path"].startswith(self.ws))

    def test_subdirectory_created_automatically(self):
        res = self._write(filename="src/models/train.py", code="x=1")
        self.assertEqual(res["status"], "success")
        self.assertTrue(Path(res["data"]["file_path"]).exists())

    def test_absolute_container_path_remapped(self):
        res = self._write(filename="/app/project/src/train.py", code="y=2")
        self.assertEqual(res["status"], "success")
        p = Path(res["data"]["file_path"])
        self.assertTrue(str(p).startswith(self.ws))
        self.assertEqual(p.read_text(), "y=2")

    def test_absolute_workdir_remapped(self):
        res = self._write(filename="run.sh", workdir="/app/project/scripts",
                          code="#!/bin/bash")
        self.assertEqual(res["status"], "success")
        p = Path(res["data"]["file_path"])
        self.assertTrue(str(p).startswith(self.ws))

    def test_overwrite_existing_file(self):
        self._write(filename="f.py", code="v=1")
        res = self._write(filename="f.py", code="v=2")
        self.assertEqual(res["status"], "success")
        self.assertEqual(Path(res["data"]["file_path"]).read_text(), "v=2")

    def test_unicode_content_written_correctly(self):
        code = "# 日本語\nprint('héllo wörld')\n"
        res = self._write(filename="unicode.py", code=code)
        self.assertEqual(res["status"], "success")
        self.assertEqual(Path(res["data"]["file_path"]).read_text(encoding="utf-8"), code)

    def test_empty_string_code_is_valid(self):
        res = self._write(filename="empty.py", code="")
        self.assertEqual(res["status"], "success")
        self.assertEqual(Path(res["data"]["file_path"]).read_text(), "")

    def test_no_thread_id_uses_default_workspace(self):
        h = make_handlers(self._td, workspace=self.ws)
        res = arun(h.handle_command({"action": "write_code", "request_id": "r",
                                      "filename": "default.py", "code": "x"}))
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["data"]["file_path"].startswith(self.ws))

    # --- error cases ---

    def test_missing_filename_returns_error(self):
        res = self._write(code="x=1")
        self.assertEqual(res["status"], "error")
        self.assertIn("filename", res["error"].lower())

    def test_missing_code_returns_error(self):
        res = arun(self.h.handle_command({"action": "write_code", "request_id": "r",
                                           "thread_id": "t1", "filename": "x.py"}))
        self.assertEqual(res["status"], "error")

    # --- path traversal ---

    def test_relative_path_traversal_blocked(self):
        # workspace is /tmp/xxx/workspace — need 3 levels up to reach /etc
        res = self._write(filename="../../../etc/passwd", code="x")
        self.assertEqual(res["status"], "error")
        self.assertIn("traversal", res["error"].lower())

    def test_deep_traversal_blocked(self):
        res = self._write(filename="../../../../etc/shadow", code="x")
        self.assertEqual(res["status"], "error")

    def test_absolute_path_outside_workspace_and_tmp_is_remapped(self):
        """Absolute path that is outside workspace goes through remap, not blocked."""
        # /etc/hostname is a real file outside workspace — after remapping it should
        # land inside workspace (filename-only fallback), NOT at /etc/hostname.
        res = self._write(filename="/etc/hostname", code="fake")
        self.assertEqual(res["status"], "success")
        p = Path(res["data"]["file_path"])
        self.assertTrue(str(p).startswith(self.ws),
                        f"Remapped path {p} escaped workspace {self.ws}")


# ---------------------------------------------------------------------------
# TestGetFileSecurity
# ---------------------------------------------------------------------------

class TestGetFileSecurity(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.ws = os.path.join(self._td, "workspace")
        os.makedirs(self.ws)
        self.legit = Path(self.ws, "legit.py")
        self.legit.write_text("# ok")
        self.h = make_handlers(self._td, workspace=self.ws,
                               thread_workspaces={"t1": self.ws})

    def _get(self, file_path, thread_id="t1"):
        return arun(self.h.handle_command({"action": "get_file", "request_id": "r",
                                            "thread_id": thread_id,
                                            "file_path": file_path}))

    def test_relative_path_in_workspace_reads_correctly(self):
        res = self._get("legit.py")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["data"]["file_content"], "# ok")

    def test_absolute_workspace_path_reads_correctly(self):
        res = self._get(str(self.legit))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["data"]["file_content"], "# ok")

    def test_missing_file_returns_error(self):
        res = self._get("nonexistent.py")
        self.assertEqual(res["status"], "error")
        self.assertIn("not found", res["error"].lower())

    def test_missing_file_path_field_returns_error(self):
        res = arun(self.h.handle_command({"action": "get_file", "request_id": "r",
                                           "thread_id": "t1"}))
        self.assertEqual(res["status"], "error")
        self.assertIn("file_path", res["error"].lower())

    # --- path traversal ---

    def test_relative_traversal_blocked(self):
        # workspace is /tmp/xxx/workspace — 3 levels up reaches /etc (outside allowed dirs)
        res = self._get("../../../etc/hostname")
        self.assertEqual(res["status"], "error")
        # Either "traversal detected" (blocked by path check) or "file not found" (after
        # safe remap to workspace where the file doesn't exist) — both are safe outcomes.
        self.assertIn(res["error"].lower(),
                      ["path traversal detected", "file not found"],
                      f"Unexpected error: {res['error']}")

    def test_deep_relative_traversal_blocked(self):
        res = self._get("../../../../etc/shadow")
        self.assertEqual(res["status"], "error")

    def test_absolute_path_outside_workspace_not_read_directly(self):
        """Absolute path outside workspace must be remapped — never read directly."""
        # /etc/hostname exists on every Linux machine and is outside workspace
        if not Path("/etc/hostname").exists():
            self.skipTest("/etc/hostname not available")
        res = self._get("/etc/hostname")
        if res["status"] == "success":
            # If it 'succeeded' the resolved path must be inside workspace (via remap)
            self.assertTrue(
                res["data"]["file_path"].startswith(self.ws),
                f"Security: read {res['data']['file_path']} which is outside workspace",
            )
        # "error" (file not found after remap) is also acceptable — it's safe

    def test_container_path_remapped_not_read_literally(self):
        """Neo sends /app/project/src/model.py — must remap, not read /app/project directly."""
        res = self._get("/app/project/src/model.py")
        # Result may be "not found" (remapped path doesn't exist) or success (if it does)
        # Either way, any success must point inside workspace
        if res["status"] == "success":
            self.assertTrue(
                res["data"]["file_path"].startswith(self.ws),
                f"Remapped path {res['data']['file_path']} escaped workspace",
            )


# ---------------------------------------------------------------------------
# TestRemapToWorkspace
# ---------------------------------------------------------------------------

class TestRemapToWorkspace(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.h = make_handlers(self._td)

    def _remap(self, path_str: str, workspace: str, workdir: str | None = None) -> Path:
        return self.h._remap_to_workspace(
            Path(path_str).resolve(), Path(workspace), workdir
        )

    def test_app_project_path(self):
        r = self._remap("/app/project/src/model.py", "/home/user/proj")
        self.assertEqual(r, Path("/home/user/proj/src/model.py"))

    def test_app_root_path(self):
        r = self._remap("/app/main.py", "/home/user/proj")
        self.assertEqual(r, Path("/home/user/proj/main.py"))

    def test_workspace_root_path(self):
        r = self._remap("/workspace/train.py", "/home/user/proj")
        self.assertEqual(r, Path("/home/user/proj/train.py"))

    def test_project_root_path(self):
        r = self._remap("/project/train.py", "/home/user/proj")
        self.assertEqual(r, Path("/home/user/proj/train.py"))

    def test_exact_root_no_trailing_slash(self):
        """Exact match /app/project (no trailing slash) → workspace root."""
        r = self._remap("/app/project", "/home/user/proj")
        self.assertEqual(str(r), "/home/user/proj")

    def test_dedup_workspace_name_in_path(self):
        """workspace ends with test_2, Neo path starts with test_2/ — strip duplicate."""
        r = self._remap("/app/project/test_2/model.py", "/home/user/test_2")
        self.assertEqual(r, Path("/home/user/test_2/model.py"))

    def test_no_dedup_when_names_differ(self):
        r = self._remap("/app/project/test_2/model.py", "/home/user/myproj")
        self.assertEqual(r, Path("/home/user/myproj/test_2/model.py"))

    def test_nested_path_preserved(self):
        r = self._remap("/app/project/a/b/c/d.py", "/home/user/proj")
        self.assertEqual(r, Path("/home/user/proj/a/b/c/d.py"))

    def test_unknown_root_falls_back_to_filename(self):
        r = self._remap("/some/random/deep/file.txt", "/home/user/proj")
        self.assertEqual(r, Path("/home/user/proj/file.txt"))

    def test_workdir_hint_used_when_matches(self):
        """workdir hint that contains the file path should strip it correctly."""
        r = self._remap("/app/project/scripts/run.sh", "/home/user/proj",
                        workdir="/app/project")
        self.assertEqual(r, Path("/home/user/proj/scripts/run.sh"))


# ---------------------------------------------------------------------------
# TestListFiles
# ---------------------------------------------------------------------------

class TestListFiles(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.ws = os.path.join(self._td, "workspace")
        os.makedirs(self.ws)
        # Structure: workspace/a.py, workspace/.hidden, workspace/sub/b.py
        Path(self.ws, "a.py").write_text("a")
        Path(self.ws, ".hidden").write_text("h")
        sub = Path(self.ws, "sub")
        sub.mkdir()
        Path(sub, "b.py").write_text("b")
        self.h = make_handlers(self._td, workspace=self.ws,
                               thread_workspaces={"t1": self.ws})

    def _list(self, **kwargs):
        return arun(self.h.handle_command({"action": "list_files", "request_id": "r",
                                            "thread_id": "t1", **kwargs}))

    def test_lists_files_in_workspace(self):
        res = self._list()
        self.assertEqual(res["status"], "success")
        stdout = res["data"]["stdout"]
        self.assertIn("a.py", stdout)
        self.assertIn("sub", stdout)
        self.assertIn("b.py", stdout)

    def test_hidden_files_excluded_by_default(self):
        res = self._list()
        self.assertNotIn(".hidden", res["data"]["stdout"])

    def test_hidden_files_included_when_requested(self):
        res = self._list(include_hidden=True)
        self.assertIn(".hidden", res["data"]["stdout"])

    def test_max_depth_zero_lists_only_top_level(self):
        res = self._list(max_depth=1)
        self.assertEqual(res["status"], "success")
        stdout = res["data"]["stdout"]
        self.assertIn("a.py", stdout)
        # sub dir appears but its contents may not depending on depth implementation
        # key assertion: b.py should NOT appear since it's at depth 2
        self.assertNotIn("b.py", stdout)

    def test_missing_directory_returns_error(self):
        res = arun(self.h.handle_command({"action": "list_files", "request_id": "r",
                                           "thread_id": "t1",
                                           "directory": "/nonexistent/path/xyz"}))
        self.assertEqual(res["status"], "error")

    def test_file_count_matches_stdout_lines(self):
        res = self._list()
        self.assertEqual(res["status"], "success")
        count = res["data"]["file_count"]
        lines = [l for l in res["data"]["stdout"].split("\n") if l.strip()]
        self.assertEqual(count, len(lines))


# ---------------------------------------------------------------------------
# TestCreateSession
# ---------------------------------------------------------------------------

class TestCreateSession(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.h = make_handlers(self._td)

    def test_with_session_id_returns_it(self):
        res = arun(self.h.handle_command({"action": "create_session", "request_id": "r",
                                           "session_id": "sess-abc"}))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["data"]["coding_session_id"], "sess-abc")

    def test_with_payload_session_id(self):
        res = arun(self.h.handle_command({"action": "create_session", "request_id": "r",
                                           "payload": {"session_id": "sess-xyz"}}))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["data"]["coding_session_id"], "sess-xyz")

    def test_without_session_id_generates_uuid(self):
        # When the backend omits session_id, a UUID is auto-generated (mirrors npm daemon).
        res = arun(self.h.handle_command({"action": "create_session", "request_id": "r"}))
        self.assertEqual(res["status"], "success")
        self.assertIn("coding_session_id", res["data"])
        self.assertTrue(len(res["data"]["coding_session_id"]) > 0)


# ---------------------------------------------------------------------------
# TestUnknownAction
# ---------------------------------------------------------------------------

class TestUnknownAction(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.h = make_handlers(self._td)

    def test_unknown_action_returns_error(self):
        res = arun(self.h.handle_command({"action": "do_something_weird",
                                           "request_id": "r"}))
        self.assertEqual(res["status"], "error")
        self.assertIn("unknown", res["error"].lower())

    def test_empty_action_returns_error(self):
        res = arun(self.h.handle_command({"action": "", "request_id": "r"}))
        self.assertEqual(res["status"], "error")

    def test_missing_action_returns_error(self):
        res = arun(self.h.handle_command({"request_id": "r"}))
        self.assertEqual(res["status"], "error")


# ---------------------------------------------------------------------------
# TestConcurrentWorkspaceIsolation
# ---------------------------------------------------------------------------

class TestConcurrentWorkspaceIsolation(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.ws = [os.path.join(self._td, f"test{i}") for i in range(1, 4)]
        for d in self.ws:
            os.makedirs(d)
        self.thread_workspaces = {f"thread-{i+1}": self.ws[i] for i in range(3)}
        self.h = ActionHandlers(
            job_manager=JobManager(),
            default_workspace=self._td,
            thread_workspaces=self.thread_workspaces,
        )

    def _cmd(self, thread_idx: int, filename: str, code: str) -> dict:
        return {"action": "write_code", "request_id": f"r-{thread_idx}",
                "thread_id": f"thread-{thread_idx}", "filename": filename, "code": code}

    # --- sequential ---

    def test_sequential_relative_writes_isolated(self):
        for i in range(1, 4):
            res = arun(self.h.handle_command(
                self._cmd(i, f"model{i}.py", f"# thread {i}")))
            self.assertEqual(res["status"], "success")
        for i, ws in enumerate(self.ws, 1):
            self.assertEqual(set(os.listdir(ws)), {f"model{i}.py"},
                             f"workspace {ws} has unexpected files")

    def test_sequential_container_path_writes_isolated(self):
        for i in range(1, 4):
            res = arun(self.h.handle_command(
                self._cmd(i, "/app/project/output.py", f"# task {i}")))
            self.assertEqual(res["status"], "success")
        for i, ws in enumerate(self.ws, 1):
            out = Path(ws, "output.py")
            self.assertTrue(out.exists(), f"Missing {out}")
            self.assertEqual(out.read_text(), f"# task {i}")

    # --- concurrent ---

    def test_concurrent_writes_no_cross_contamination(self):
        async def _run():
            return await asyncio.gather(*[
                self.h.handle_command(
                    self._cmd(i, "/app/project/output.py", f"# task {i}"))
                for i in range(1, 4)
            ])
        results = asyncio.run(_run())
        for i, res in enumerate(results, 1):
            self.assertEqual(res["status"], "success", f"task {i}: {res}")
        for i, ws in enumerate(self.ws, 1):
            self.assertEqual(Path(ws, "output.py").read_text(), f"# task {i}")

    def test_concurrent_many_files_per_thread(self):
        """Each thread writes 5 files concurrently — no files land in wrong workspace."""
        async def _run():
            cmds = [
                self.h.handle_command(
                    self._cmd(tid, f"file_{n}.py", f"# t{tid} f{n}"))
                for tid in range(1, 4)
                for n in range(5)
            ]
            return await asyncio.gather(*cmds)
        results = asyncio.run(_run())
        self.assertTrue(all(r["status"] == "success" for r in results))
        # Each workspace must have exactly 5 files
        for ws in self.ws:
            self.assertEqual(len(os.listdir(ws)), 5,
                             f"{ws} has {os.listdir(ws)}")

    def test_unknown_thread_falls_back_to_default_workspace(self):
        res = arun(self.h.handle_command({"action": "write_code", "request_id": "r",
                                           "thread_id": "unknown-thread",
                                           "filename": "x.py", "code": "y"}))
        self.assertEqual(res["status"], "success")
        p = Path(res["data"]["file_path"])
        self.assertTrue(str(p).startswith(self._td))
        # Must NOT be in any of the three registered workspaces
        for ws in self.ws:
            self.assertFalse(str(p).startswith(ws),
                             f"Unknown thread wrote into registered workspace {ws}")


# ---------------------------------------------------------------------------
# TestJobCleanup
# ---------------------------------------------------------------------------

class TestJobCleanup(unittest.TestCase):

    def test_removes_old_completed_job(self):
        jm = JobManager()
        jm._jobs["old"] = _fake_job("old", exit_code=0, hours_old=25)
        jm.cleanup_old_jobs()
        self.assertNotIn("old", jm._jobs)

    def test_removes_old_failed_job(self):
        jm = JobManager()
        jm._jobs["old-fail"] = _fake_job("old-fail", exit_code=1, hours_old=25)
        jm.cleanup_old_jobs()
        self.assertNotIn("old-fail", jm._jobs)

    def test_keeps_recent_completed_job(self):
        jm = JobManager()
        jm._jobs["recent"] = _fake_job("recent", exit_code=0, hours_old=1)
        jm.cleanup_old_jobs()
        self.assertIn("recent", jm._jobs)

    def test_keeps_running_old_job(self):
        """Still-running jobs (exit_code=None) must never be evicted."""
        jm = JobManager()
        jm._jobs["running"] = _fake_job("running", exit_code=None, hours_old=30)
        jm.cleanup_old_jobs()
        self.assertIn("running", jm._jobs)

    def test_cleanup_empty_registry_is_noop(self):
        jm = JobManager()
        jm.cleanup_old_jobs()  # must not raise
        self.assertEqual(len(jm._jobs), 0)

    def test_mixed_jobs_only_old_completed_removed(self):
        jm = JobManager()
        jm._jobs["old-done"] = _fake_job("old-done", exit_code=0, hours_old=25)
        jm._jobs["new-done"] = _fake_job("new-done", exit_code=0, hours_old=1)
        jm._jobs["old-running"] = _fake_job("old-running", exit_code=None, hours_old=25)
        jm.cleanup_old_jobs()
        self.assertNotIn("old-done", jm._jobs)
        self.assertIn("new-done", jm._jobs)
        self.assertIn("old-running", jm._jobs)

    def test_get_job_logs_returns_none_for_cleaned_job(self):
        jm = JobManager()
        jm._jobs["old"] = _fake_job("old", exit_code=0, hours_old=25)
        jm.cleanup_old_jobs()
        self.assertIsNone(jm.get_job_logs("old"))


# ---------------------------------------------------------------------------
# TestWorkspaceRegistration
# ---------------------------------------------------------------------------

class TestWorkspaceRegistration(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.tw: dict[str, str] = {}
        self.h = make_handlers(self._td, thread_workspaces=self.tw)

    def test_unknown_thread_returns_default(self):
        self.assertEqual(self.h._workspace_for("nope"), self._td)

    def test_registered_thread_returns_its_workspace(self):
        ws = os.path.join(self._td, "custom")
        os.makedirs(ws)
        self.tw["t-abc"] = ws
        self.assertEqual(self.h._workspace_for("t-abc"), ws)

    def test_multiple_threads_fully_isolated(self):
        ws_a = os.path.join(self._td, "a")
        ws_b = os.path.join(self._td, "b")
        self.tw["ta"] = ws_a
        self.tw["tb"] = ws_b
        self.assertNotEqual(self.h._workspace_for("ta"), self.h._workspace_for("tb"))
        self.assertEqual(self.h._workspace_for("ta"), ws_a)
        self.assertEqual(self.h._workspace_for("tb"), ws_b)

    def test_runtime_registration_reflected_immediately(self):
        """Adding to the shared dict is immediately visible (same object reference)."""
        ws = os.path.join(self._td, "late")
        os.makedirs(ws)
        self.assertEqual(self.h._workspace_for("late-thread"), self._td)  # default
        self.tw["late-thread"] = ws
        self.assertEqual(self.h._workspace_for("late-thread"), ws)  # now registered

    def test_none_thread_id_returns_default(self):
        self.assertEqual(self.h._workspace_for(None), self._td)


if __name__ == "__main__":
    unittest.main()
