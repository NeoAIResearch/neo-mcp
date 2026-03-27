"""Tests for neo_mcp.setup — setup wizard and VS Code extension detection.

Coverage:
  - _vscode_extension_deployment_id: daemon.log parsing, fallbacks, edge cases
  - run_setup: extension present → skip login + daemon
  - run_setup: no extension + no existing token → login triggered
  - run_setup: no extension + existing token → login skipped
  - run_setup: remote mode + extension → daemon not started
  - run_setup: remote mode + no extension → daemon started
  - run_setup: deployment_id from extension used in editor config
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
# 1. _vscode_extension_deployment_id
# ---------------------------------------------------------------------------

class TestVscodeExtensionDeploymentId(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._daemon_dir = _make_daemon_dir(self._tmpdir)
        # Patch _DAEMON_DIR so all path operations hit our temp directory
        self._patcher = patch.object(setup_mod, "_DAEMON_DIR", self._daemon_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_returns_id_from_daemon_log(self):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _write_log(self._daemon_dir, [f'{{"sandboxId": "{uid}"}}'])
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), uid)

    def test_returns_last_entry_when_multiple(self):
        uid1 = "11111111-1111-1111-1111-111111111111"
        uid2 = "22222222-2222-2222-2222-222222222222"
        _write_log(self._daemon_dir, [
            f'{{"sandboxId": "{uid1}"}}',
            f'{{"sandboxId": "{uid2}"}}',
        ])
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), uid2)

    def test_falls_back_to_daemon_log_1(self):
        uid = "33333333-3333-3333-3333-333333333333"
        _write_log(self._daemon_dir, [f'{{"sandboxId": "{uid}"}}'], "daemon.log.1")
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), uid)

    def test_prefers_daemon_log_over_log_1(self):
        uid1 = "44444444-4444-4444-4444-444444444444"
        uid2 = "55555555-5555-5555-5555-555555555555"
        _write_log(self._daemon_dir, [f'{{"sandboxId": "{uid1}"}}'], "daemon.log")
        _write_log(self._daemon_dir, [f'{{"sandboxId": "{uid2}"}}'], "daemon.log.1")
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), uid1)

    def test_returns_empty_when_no_log_files(self):
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), "")

    def test_returns_empty_for_short_uuid(self):
        _write_log(self._daemon_dir, ['{"sandboxId": "tooshort"}'])
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), "")

    def test_handles_mixed_log_lines(self):
        uid = "66666666-6666-6666-6666-666666666666"
        _write_log(self._daemon_dir, [
            "[2026-01-01] Daemon started",
            '{"other": "data"}',
            f'{{"sandboxId": "{uid}"}}',
            "[2026-01-01] Still running",
        ])
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), uid)

    def test_empty_log_file_returns_empty(self):
        _write_log(self._daemon_dir, [])
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), "")

    def test_recovers_from_log_with_no_sandbox_id(self):
        """daemon.log has no sandboxId, fall back to daemon.log.1."""
        uid = "77777777-7777-7777-7777-777777777777"
        _write_log(self._daemon_dir, ["[2026-01-01] Just startup messages"], "daemon.log")
        _write_log(self._daemon_dir, [f'{{"sandboxId": "{uid}"}}'], "daemon.log.1")
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), uid)

    def test_ignores_invalid_uuid_in_log(self):
        _write_log(self._daemon_dir, ['{"sandboxId": "not-a-real-uuid"}'])
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), "")

    def test_valid_uuid_36_chars(self):
        """Exact 36-char UUID must be accepted."""
        uid = "abcdef12-3456-7890-abcd-ef1234567890"
        _write_log(self._daemon_dir, [f'{{"sandboxId": "{uid}"}}'])
        result = setup_mod._vscode_extension_deployment_id()
        self.assertEqual(result, uid)


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
        patch("neo_mcp.setup._vscode_extension_deployment_id", return_value=vscode_id),
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

    def test_extension_id_passed_to_editor_config(self):
        ext_id = "eeeeeeee-ffff-0000-1111-222222222222"
        captured_opts = {}

        def fake_configurator(sk, opts):
            captured_opts.update(opts)
            return (True, "ok")

        args = ["--secret-key", "sk-v1-testkey", "--editor", "claude", "--remote"]
        patches = _base_patches(vscode_id=ext_id, has_token=False, tmpdir=self._tmpdir, remote=True)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], \
             patch("neo_mcp.setup._CONFIGURATORS", {"claude": fake_configurator}), \
             patch("neo_mcp.setup._do_login", return_value=True), \
             patch("neo_mcp.setup._start_daemon", return_value=True), \
             patch("sys.stdin.isatty", return_value=True):
            setup_mod.run_setup(args)

        self.assertEqual(captured_opts.get("deployment_id"), ext_id)


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

    def test_no_extension_no_token_login_called(self):
        mock_login, _ = self._run(has_token=False, remote=False)
        mock_login.assert_called_once()

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

    def test_no_extension_login_failure_continues_setup(self):
        """Login failure should not abort setup — user can run neo-mcp login later."""
        mock_login, _ = self._run(has_token=False, remote=False, login_succeeds=False)
        mock_login.assert_called_once()

    def test_no_extension_key_derived_id_used_remote(self):
        """Without extension, deployment_id is derived from API key."""
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
        # Must be a non-empty string (the key-derived UUID)
        self.assertTrue(dep_id, "deployment_id should be set in remote mode")
        # Must differ from extension ID (which is empty here)
        self.assertNotEqual(dep_id, "")


# ---------------------------------------------------------------------------
# 3. _vscode_extension_deployment_id vs _discover_sandbox_id parity
# ---------------------------------------------------------------------------

class TestExtensionVsDiscoverParity(unittest.TestCase):
    """_vscode_extension_deployment_id and server._vscode_daemon_deployment_id
    must agree on the same daemon.log format."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._daemon_dir = _make_daemon_dir(self._tmpdir)
        self._setup_patcher = patch.object(setup_mod, "_DAEMON_DIR", self._daemon_dir)
        self._setup_patcher.start()

        import neo_mcp.server as srv
        self._srv = srv
        self._srv_patcher = patch("neo_mcp.server.os.path.expanduser",
                                  side_effect=lambda p: p.replace("~", self._tmpdir))
        self._srv_patcher.start()

    def tearDown(self):
        self._setup_patcher.stop()
        self._srv_patcher.stop()

    def test_both_agree_on_valid_entry(self):
        uid = "abcdef12-3456-7890-abcd-ef1234567890"
        _write_log(self._daemon_dir, [f'{{"sandboxId": "{uid}"}}'])
        self.assertEqual(
            setup_mod._vscode_extension_deployment_id(),
            self._srv._vscode_daemon_deployment_id(),
        )

    def test_both_return_empty_for_no_log(self):
        self.assertEqual(setup_mod._vscode_extension_deployment_id(), "")
        self.assertEqual(self._srv._vscode_daemon_deployment_id(), "")

    def test_both_return_last_when_multiple_entries(self):
        uid1 = "11111111-1111-1111-1111-111111111111"
        uid2 = "22222222-2222-2222-2222-222222222222"
        _write_log(self._daemon_dir, [
            f'{{"sandboxId": "{uid1}"}}',
            f'{{"sandboxId": "{uid2}"}}',
        ])
        r_setup = setup_mod._vscode_extension_deployment_id()
        r_srv = self._srv._vscode_daemon_deployment_id()
        self.assertEqual(r_setup, uid2)
        self.assertEqual(r_srv, uid2)
        self.assertEqual(r_setup, r_srv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
