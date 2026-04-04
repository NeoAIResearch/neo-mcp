"""Tests for CLI UX helpers (doctor/status/list/logs/self-test)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import neo_mcp.server as srv


class TestCliUxHelpers(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.mkdtemp()
        self._orig_home = os.environ.get("HOME")
        self._orig_dep = os.environ.get("NEO_DEPLOYMENT_ID")
        self._orig_mode = os.environ.get("NEO_DEPLOYMENT_ID_MODE")
        self._orig_key = os.environ.get("NEO_SECRET_KEY")
        os.environ["HOME"] = self._td
        os.environ.pop("NEO_DEPLOYMENT_ID", None)
        os.environ.pop("NEO_DEPLOYMENT_ID_MODE", None)
        os.environ.pop("NEO_SECRET_KEY", None)
        self._orig_daemon_dir = srv.DAEMON_DIR
        self._orig_daemon_log = srv.DAEMON_LOG
        self._orig_lock = srv.LOCK_FILE
        self._orig_pid = srv.PID_FILE
        self._orig_standalone = srv.STANDALONE_UUID_FILE
        self._orig_workspaces = srv.THREAD_WORKSPACES_FILE
        daemon_dir = Path(self._td) / ".neo" / "daemon"
        srv.DAEMON_DIR = daemon_dir
        srv.DAEMON_LOG = daemon_dir / "daemon.log"
        srv.LOCK_FILE = daemon_dir / "neo-mcp.lock"
        srv.PID_FILE = daemon_dir / "neo-mcp.pid"
        srv.STANDALONE_UUID_FILE = daemon_dir / "standalone_deployment_id"
        srv.THREAD_WORKSPACES_FILE = daemon_dir / "thread-workspaces.json"

    def tearDown(self):
        if self._orig_home is not None:
            os.environ["HOME"] = self._orig_home
        else:
            os.environ.pop("HOME", None)
        if self._orig_dep is not None:
            os.environ["NEO_DEPLOYMENT_ID"] = self._orig_dep
        else:
            os.environ.pop("NEO_DEPLOYMENT_ID", None)
        if self._orig_mode is not None:
            os.environ["NEO_DEPLOYMENT_ID_MODE"] = self._orig_mode
        else:
            os.environ.pop("NEO_DEPLOYMENT_ID_MODE", None)
        if self._orig_key is not None:
            os.environ["NEO_SECRET_KEY"] = self._orig_key
        else:
            os.environ.pop("NEO_SECRET_KEY", None)
        srv.DAEMON_DIR = self._orig_daemon_dir
        srv.DAEMON_LOG = self._orig_daemon_log
        srv.LOCK_FILE = self._orig_lock
        srv.PID_FILE = self._orig_pid
        srv.STANDALONE_UUID_FILE = self._orig_standalone
        srv.THREAD_WORKSPACES_FILE = self._orig_workspaces

    def test_deployment_id_source_explicit(self):
        os.environ["NEO_DEPLOYMENT_ID"] = "dep-123"
        dep, source = srv._deployment_id_source("sk-v1-test")
        self.assertEqual(dep, "dep-123")
        self.assertEqual(source, "explicit-env")

    def test_deployment_id_source_key_mode(self):
        os.environ["NEO_DEPLOYMENT_ID_MODE"] = "key-derived"
        dep, source = srv._deployment_id_source("sk-v1-test")
        self.assertRegex(dep, r"^[a-f0-9\-]{36}$")
        self.assertEqual(source, "key-derived-mode")

    def test_status_json_output(self):
        os.environ["NEO_SECRET_KEY"] = "sk-v1-test"
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = srv._cmd_status(json_mode=True)
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertIn("deployment_id_source", data)
        self.assertEqual(data["http_mode"], "obsolete-not-used")

    def test_doctor_returns_nonzero_without_key(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = srv._cmd_doctor(json_mode=True)
        self.assertEqual(rc, 1)

    def test_self_test_passes(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = srv._cmd_self_test(json_mode=True)
        self.assertEqual(rc, 0)

    def test_logs_missing_file_returns_error(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = srv._cmd_logs(lines=10, source="neo-mcp")
        self.assertEqual(rc, 1)
        self.assertIn("No log file found", out.getvalue())

    def test_tail_alias_matches_logs_behavior(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = srv._cmd_tail(lines=10, source="neo-mcp")
        self.assertEqual(rc, 1)
        self.assertIn("No log file found", out.getvalue())

    def test_list_reads_thread_workspaces(self):
        daemon_dir = Path(self._td) / ".neo" / "daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        workspaces = daemon_dir / "thread-workspaces.json"
        workspaces.write_text(json.dumps({"t1": {"workspace": "/tmp/w1", "updated_at": 1}}))
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = srv._cmd_list(json_mode=True)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tasks"][0]["thread_id"], "t1")


if __name__ == "__main__":
    unittest.main()
