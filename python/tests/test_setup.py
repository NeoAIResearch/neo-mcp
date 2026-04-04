"""Tests for neo_mcp.setup — setup wizard and VS Code extension detection.

Coverage:
  - _vscode_extension_deployment_id: daemon.log parsing, fallbacks, edge cases
  - run_setup: extension present → skip login + daemon
  - run_setup: no extension + no existing token → login triggered
  - run_setup: no extension + existing token → login skipped
  - run_setup: remote mode + extension → daemon not started
  - run_setup: remote mode + no extension → daemon started
  - run_setup: machine deployment_id passed to editor config
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import neo_mcp.setup as setup_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_daemon_dir(tmpdir: str) -> str:
    d = os.path.join(tmpdir, ".neo", "daemon")
    os.makedirs(d, exist_ok=True)
    return d


def _write_log(daemon_dir: str, lines: list[str], filename: str = "daemon.log") -> None:
    path = os.path.join(daemon_dir, filename)
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _write_auth(daemon_dir: str, token: str = "oauth-token-xyz", username: str = "testuser") -> None:
    path = os.path.join(daemon_dir, "mcp_auth.json")
    with open(path, "w") as f:
        json.dump({"access_token": token, "username": username}, f)


# ---------------------------------------------------------------------------
# 1. _vscode_extension_running — socket + daemon.token check
# ---------------------------------------------------------------------------

class TestVscodeExtensionRunning(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._daemon_dir = _make_daemon_dir(self._tmpdir)
        self._patcher = patch.object(setup_mod, "_DAEMON_DIR", self._daemon_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _write_token(self) -> None:
        (Path(self._daemon_dir) / "daemon.token").write_text("test-token")

    def test_returns_false_when_no_token_file(self):
        """Fast path: no daemon.token → extension not running, no socket attempt."""
        import socket as _socket
        with patch.object(_socket, "socket") as mock_sock:
            result = setup_mod._vscode_extension_running()
        self.assertFalse(result)
        mock_sock.assert_not_called()  # Socket never created

    def test_returns_false_when_token_exists_but_port_closed(self):
        """Token file exists but nothing on port 31337 → not running."""
        self._write_token()
        import socket as _socket

        mock_s = MagicMock()
        mock_s.connect.side_effect = OSError("Connection refused")
        with patch.object(_socket, "socket", return_value=mock_s):
            result = setup_mod._vscode_extension_running()
        self.assertFalse(result)

    def test_returns_true_when_token_exists_and_port_open(self):
        """Token file exists and port 31337 accepts connection → running."""
        self._write_token()
        import socket as _socket

        mock_s = MagicMock()
        mock_s.connect.return_value = None  # Success
        with patch.object(_socket, "socket", return_value=mock_s):
            result = setup_mod._vscode_extension_running()
        self.assertTrue(result)
        mock_s.connect.assert_called_once_with(("127.0.0.1", 31337))

    def test_timeout_set_to_one_second(self):
        """Socket timeout must be 1s to avoid blocking setup."""
        self._write_token()
        import socket as _socket

        mock_s = MagicMock()
        mock_s.connect.return_value = None
        with patch.object(_socket, "socket", return_value=mock_s):
            setup_mod._vscode_extension_running()
        mock_s.settimeout.assert_called_once_with(1.0)

    def test_socket_closed_after_connect(self):
        """Socket must be closed after successful connection check."""
        self._write_token()
        import socket as _socket

        mock_s = MagicMock()
        mock_s.connect.return_value = None
        with patch.object(_socket, "socket", return_value=mock_s):
            setup_mod._vscode_extension_running()
        mock_s.close.assert_called_once()


# ---------------------------------------------------------------------------
# 2. run_setup — extension detection paths
# ---------------------------------------------------------------------------

def _base_patches(vscode_id: str, has_token: bool, tmpdir: str, remote: bool = False):
    """Return a list of context-manager patches common to run_setup tests."""
    daemon_dir = os.path.join(tmpdir, ".neo", "daemon")
    auth_file = os.path.join(daemon_dir, "mcp_auth.json")
    standalone_file = os.path.join(daemon_dir, "standalone_deployment_id")
    return [
        patch.object(setup_mod, "_DAEMON_DIR", daemon_dir),
        patch.object(setup_mod, "_MCP_AUTH_FILE", auth_file),
        patch.object(setup_mod, "_STANDALONE_UUID_FILE", standalone_file),
        patch("neo_mcp.setup._vscode_extension_running", return_value=bool(vscode_id)),
        patch("neo_mcp.setup._validate_api_key", return_value=True),
        patch("neo_mcp.setup._valid_oauth_token", return_value="tok" if has_token else ""),
        patch("neo_mcp.setup._daemon_running", return_value=False),
        patch("neo_mcp.setup._ask_remote", return_value=remote),
        # Suppress all editor configurators
        patch("neo_mcp.setup._CONFIGURATORS", {"claude": lambda sk, opts: (True, "ok")}),
    ]


class TestRunSetupExtensionDetected(unittest.TestCase):
    """When VS Code/Cursor extension daemon.log is present, login and Python daemon are skipped."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self._tmpdir, ".neo", "daemon"), exist_ok=True)

    def _run(self, extra_args: list[str] = None, remote: bool = False):
        args = ["--secret-key", "sk-v1-testkey", "--editor", "claude"]
        if remote:
            args.append("--remote")
        if extra_args:
            args.extend(extra_args)

        ext_id = "eeeeeeee-ffff-0000-1111-222222222222"
        patches = _base_patches(vscode_id=ext_id, has_token=False, tmpdir=self._tmpdir, remote=remote)

        mock_login = MagicMock(return_value=True)
        mock_start_daemon = MagicMock(return_value=True)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], \
             patch("neo_mcp.setup._do_login", mock_login), \
             patch("neo_mcp.setup._start_daemon", mock_start_daemon), \
             patch("sys.stdin.isatty", return_value=True):
            setup_mod.run_setup(args)

        return mock_login, mock_start_daemon, ext_id

    def test_extension_detected_login_not_called(self):
        mock_login, _, _ = self._run()
        mock_login.assert_not_called()

    def test_extension_detected_daemon_not_started_local_mode(self):
        _, mock_daemon, _ = self._run(remote=False)
        mock_daemon.assert_not_called()

    def test_extension_detected_daemon_not_started_remote_mode(self):
        _, mock_daemon, _ = self._run(remote=True)
        mock_daemon.assert_not_called()

    def test_extension_detected_sets_deployment_id_for_remote_config(self):
        """When extension is running, setup still passes deployment_id for HTTP routing."""
        captured_opts = {}

        def fake_configurator(sk, opts):
            captured_opts.update(opts)
            return (True, "ok")

        args = ["--secret-key", "sk-v1-testkey", "--editor", "claude", "--remote"]
        patches = _base_patches(vscode_id="anything", has_token=False, tmpdir=self._tmpdir, remote=True)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], \
             patch("neo_mcp.setup._CONFIGURATORS", {"claude": fake_configurator}), \
             patch("neo_mcp.setup._do_login", return_value=True), \
             patch("neo_mcp.setup._start_daemon", return_value=True), \
             patch("sys.stdin.isatty", return_value=True):
            setup_mod.run_setup(args)

        dep_id = captured_opts.get("deployment_id", "")
        # Must be non-empty UUID-like value for X-Neo-Deployment-Id routing
        self.assertTrue(dep_id, "deployment_id must be set in remote mode")
        import re
        self.assertRegex(dep_id, r"^[a-f0-9\-]{36}$")


class TestRunSetupNoExtension(unittest.TestCase):
    """When no VS Code extension, login and daemon behave as before."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self._tmpdir, ".neo", "daemon"), exist_ok=True)

    def _run(self, has_token: bool, remote: bool, login_succeeds: bool = True):
        args = ["--secret-key", "sk-v1-testkey", "--editor", "claude"]
        if remote:
            args.append("--remote")

        patches = _base_patches(vscode_id="", has_token=has_token, tmpdir=self._tmpdir, remote=remote)
        mock_login = MagicMock(return_value=login_succeeds)
        mock_start_daemon = MagicMock(return_value=True)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], \
             patch("neo_mcp.setup._do_login", mock_login), \
             patch("neo_mcp.setup._start_daemon", mock_start_daemon), \
             patch("sys.stdin.isatty", return_value=True):
            setup_mod.run_setup(args)

        return mock_login, mock_start_daemon

    def test_no_extension_login_not_called_api_key_auth(self):
        """Login is no longer required — daemon authenticates with API key."""
        mock_login, _ = self._run(has_token=False, remote=False)
        mock_login.assert_not_called()

    def test_no_extension_existing_token_login_skipped(self):
        mock_login, _ = self._run(has_token=True, remote=False)
        mock_login.assert_not_called()

    def test_no_extension_remote_mode_daemon_started(self):
        _, mock_daemon = self._run(has_token=True, remote=True)
        mock_daemon.assert_called_once()

    def test_no_extension_local_mode_daemon_not_started_by_setup(self):
        """In local (stdio) mode, setup doesn't pre-start the daemon."""
        _, mock_daemon = self._run(has_token=True, remote=False)
        mock_daemon.assert_not_called()

    def test_no_extension_setup_completes_without_login(self):
        """Setup completes without OAuth login — API key is sufficient."""
        mock_login, _ = self._run(has_token=False, remote=False, login_succeeds=False)
        mock_login.assert_not_called()

    def test_no_extension_machine_id_used_remote(self):
        """Without extension, setup passes machine deployment_id in remote mode."""
        captured_opts = {}

        def fake_configurator(sk, opts):
            captured_opts.update(opts)
            return (True, "ok")

        args = ["--secret-key", "sk-v1-testkey", "--editor", "claude", "--remote"]
        patches = _base_patches(vscode_id="", has_token=True, tmpdir=self._tmpdir, remote=True)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], \
             patch("neo_mcp.setup._CONFIGURATORS", {"claude": fake_configurator}), \
             patch("neo_mcp.setup._do_login", return_value=True), \
             patch("neo_mcp.setup._start_daemon", return_value=True), \
             patch("sys.stdin.isatty", return_value=True):
            setup_mod.run_setup(args)

        dep_id = captured_opts.get("deployment_id", "")
        # Must be a non-empty UUID-like value
        self.assertTrue(dep_id, "deployment_id should be set in remote mode")
        import re
        self.assertRegex(dep_id, r"^[a-f0-9\-]{36}$")


# ---------------------------------------------------------------------------
# 3. Remote config wiring: X-Neo-Deployment-Id header
# ---------------------------------------------------------------------------

class TestRemoteConfigHeaders(unittest.TestCase):
    def test_configure_claude_cli_includes_deployment_header(self):
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            ok, _ = setup_mod._configure_claude("sk-v1-test", {
                "remote": True,
                "scope": "user",
                "deployment_id": "11111111-2222-3333-4444-555555555555",
            })
        self.assertTrue(ok)
        cmd = mock_run.call_args.args[0]
        joined = " ".join(cmd)
        self.assertIn("Authorization: Bearer sk-v1-test", joined)
        self.assertIn("X-Neo-Deployment-Id: 11111111-2222-3333-4444-555555555555", joined)

    def test_configure_cursor_remote_writes_deployment_header(self):
        tmpdir = tempfile.mkdtemp()
        fake_home = Path(tmpdir)
        with patch.object(Path, "home", return_value=fake_home):
            ok, _ = setup_mod._configure_cursor("sk-v1-test", {
                "remote": True,
                "deployment_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "no_backup": True,
            })
            self.assertTrue(ok)
            data = json.loads((fake_home / ".cursor" / "mcp.json").read_text())
            headers = data["mcpServers"]["neo"]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer sk-v1-test")
            self.assertEqual(headers["X-Neo-Deployment-Id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_configure_windsurf_remote_writes_deployment_header(self):
        tmpdir = tempfile.mkdtemp()
        fake_home = Path(tmpdir)
        with patch.object(Path, "home", return_value=fake_home):
            ok, _ = setup_mod._configure_windsurf("sk-v1-test", {
                "remote": True,
                "deployment_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                "no_backup": True,
            })
            self.assertTrue(ok)
            data = json.loads((fake_home / ".codeium" / "windsurf" / "mcp_config.json").read_text())
            headers = data["mcpServers"]["neo"]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer sk-v1-test")
            self.assertEqual(headers["X-Neo-Deployment-Id"], "bbbbbbbb-cccc-dddd-eeee-ffffffffffff")

    def test_configure_vscode_remote_writes_deployment_header(self):
        tmpdir = tempfile.mkdtemp()
        cwd = Path(tmpdir)
        with patch.object(Path, "cwd", return_value=cwd):
            ok, _ = setup_mod._configure_vscode("sk-v1-test", {
                "remote": True,
                "deployment_id": "99999999-aaaa-bbbb-cccc-dddddddddddd",
                "no_backup": True,
            })
            self.assertTrue(ok)
            data = json.loads((cwd / ".vscode" / "mcp.json").read_text())
            headers = data["servers"]["neo"]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer sk-v1-test")
            self.assertEqual(headers["X-Neo-Deployment-Id"], "99999999-aaaa-bbbb-cccc-dddddddddddd")


# ---------------------------------------------------------------------------
# 4. Detection correctness: _vscode_extension_running vs daemon.log approach
# ---------------------------------------------------------------------------

class TestExtensionDetectionCorrectness(unittest.TestCase):
    """Verify that _vscode_extension_running() correctly detects the live extension
    and is NOT fooled by stale daemon.log entries from the Python daemon."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._daemon_dir = _make_daemon_dir(self._tmpdir)
        self._patcher = patch.object(setup_mod, "_DAEMON_DIR", self._daemon_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _write_token(self):
        (Path(self._daemon_dir) / "daemon.token").write_text("test-token")

    def test_stale_python_daemon_log_does_not_trigger_extension_detection(self):
        """Python daemon writes sandboxId to daemon.log. Even with that entry present,
        _vscode_extension_running() must return False when port 31337 is closed."""
        uid = "deadbeef-0000-0000-0000-000000000000"
        _write_log(self._daemon_dir,
                   [f'{{"sandboxId": "{uid}", "source": "python-daemon"}}'])
        # daemon.token absent → no extension
        result = setup_mod._vscode_extension_running()
        self.assertFalse(result)

    def test_extension_detected_by_live_port_not_log(self):
        """Extension running = daemon.token + port 31337 open, regardless of daemon.log."""
        self._write_token()
        import socket as _socket
        mock_s = MagicMock()
        mock_s.connect.return_value = None
        with patch.object(_socket, "socket", return_value=mock_s):
            result = setup_mod._vscode_extension_running()
        self.assertTrue(result)

    def test_dead_extension_token_file_remains_returns_false(self):
        """Token file can persist after extension exits. Must still return False."""
        self._write_token()
        import socket as _socket
        mock_s = MagicMock()
        mock_s.connect.side_effect = OSError("Connection refused")
        with patch.object(_socket, "socket", return_value=mock_s):
            result = setup_mod._vscode_extension_running()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
