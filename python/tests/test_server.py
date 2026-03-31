"""Comprehensive unit tests for neo_mcp.server — no network calls required.

Coverage:
  - Error handling (all HTTP status codes)
  - Auth / headers (env key, context var, missing key)
  - Thread ID persistence (save/load/recover/strip)
  - Thread ID resolution (supplied, stored, unknown, empty)
  - Sandbox ID discovery (daemon.log, thread-workspaces.json, both missing)
  - Deployment ID resolution (env override, discovery)
  - Message pagination (_fetch_messages_pages)
  - Background poller (_poll_task_bg) — status transitions, adaptive delay, terminal states
  - Tool: neo_submit_task — success, 400, no deployment_id, wait_for_completion
  - Tool: neo_task_status — cache hit, API fallback, no thread_id, plan rendering
  - Tool: neo_task_plan   — cache hit, API fallback, no plan yet
  - Tool: neo_get_messages — cached, API fetch, empty, capped
  - Tool: neo_get_files   — success (export + poll + download), export fail, no files
  - Tool: neo_send_feedback — success, error, optimistic cache update
  - Tool: neo_pause_task  — success, error, cache update
  - Tool: neo_resume_task — success, error, cache update
  - Tool: neo_stop_task   — success, error, cache cleared
  - READ_ONLY mode — write tools absent from list_tools
  - HTTP Bearer token extraction (leading/trailing spaces)
  - Unknown tool name returns error text
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

# Provide a dummy key so the module can import without raising at startup
os.environ["NEO_SECRET_KEY"] = "sk-v1-test"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import neo_mcp.server as srv
import mcp.types as mcp_types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def call_tool(name: str, arguments: dict | None = None):
    """Invoke the MCP call_tool handler synchronously for testing."""
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    handler = srv.app.request_handlers[mcp_types.CallToolRequest]
    result = asyncio.get_event_loop().run_until_complete(handler(req))
    # result is a ServerResult wrapping a CallToolResult
    return result.root.content  # list[TextContent]


def text_of(result) -> str:
    """Extract concatenated text from a list[TextContent]."""
    return "\n".join(c.text for c in result if hasattr(c, "text"))


def make_response(status_code: int, body: dict | str = "") -> MagicMock:
    """Create a mock httpx Response."""
    r = MagicMock()
    r.status_code = status_code
    if isinstance(body, dict):
        r.json.return_value = body
        r.text = json.dumps(body)
    else:
        r.text = body
        r.json.side_effect = ValueError("not json")
    return r


def make_async_client(responses: dict[str, MagicMock]) -> MagicMock:
    """Return a mock httpx.AsyncClient context manager.

    `responses` maps HTTP method+path keys to mock Response objects.
    A fallback "DEFAULT" key can be used for any unmatched request.
    """
    mock_client = AsyncMock()

    def dispatch(method, url_or_path, **kwargs):
        """Sync side_effect: AsyncMock calls this and returns the result directly."""
        key = f"{method.upper()} {url_or_path}"
        resp = responses.get(key)
        if resp is None:
            for k, v in responses.items():
                if k != "DEFAULT" and url_or_path.endswith(k.split(" ", 1)[-1]):
                    resp = v
                    break
        if resp is None:
            resp = responses.get("DEFAULT", make_response(200, {}))
        return resp

    mock_client.get = AsyncMock(side_effect=lambda url, **kw: dispatch("GET", url, **kw))
    mock_client.post = AsyncMock(side_effect=lambda url, **kw: dispatch("POST", url, **kw))
    mock_client.delete = AsyncMock(side_effect=lambda url, **kw: dispatch("DELETE", url, **kw))

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_client


# ---------------------------------------------------------------------------
# 1. Error handling
# ---------------------------------------------------------------------------

class TestHandleError(unittest.TestCase):
    def test_known_codes(self):
        self.assertIn("Invalid API key", srv.handle_error(401))
        self.assertIn("insufficient credits", srv.handle_error(402))
        self.assertIn("trial or quota", srv.handle_error(403))
        self.assertIn("Thread or user not found", srv.handle_error(404))
        self.assertIn("Too many requests", srv.handle_error(429))
        self.assertIn("backend error", srv.handle_error(500))
        self.assertIn("unavailable", srv.handle_error(502))
        self.assertIn("unavailable", srv.handle_error(503))
        self.assertIn("timed out", srv.handle_error(504))

    def test_unknown_code(self):
        msg = srv.handle_error(418)
        self.assertIn("418", msg)

    def test_400_deployment_message(self):
        msg = srv.handle_error(400)
        self.assertIn("daemon", msg.lower())
        self.assertIn("neo-mcp-daemon", msg)

    def test_all_codes_return_nonempty_string(self):
        for code in [400, 401, 402, 403, 404, 429, 500, 502, 503, 504]:
            self.assertIsInstance(srv.handle_error(code), str)
            self.assertTrue(len(srv.handle_error(code)) > 0)


# ---------------------------------------------------------------------------
# 2. Auth headers
# ---------------------------------------------------------------------------

class TestHeaders(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._ctx_secret_key.set("")

    def test_uses_env_key(self):
        srv.NEO_SECRET_KEY = "sk-v1-envkey"
        headers = srv._headers()
        self.assertEqual(headers["Authorization"], "Bearer sk-v1-envkey")

    def test_ctx_key_takes_priority(self):
        srv.NEO_SECRET_KEY = "sk-v1-env"
        token = srv._ctx_secret_key.set("sk-v1-ctx")
        headers = srv._headers()
        self.assertEqual(headers["Authorization"], "Bearer sk-v1-ctx")
        srv._ctx_secret_key.reset(token)

    def test_raises_when_no_key(self):
        srv.NEO_SECRET_KEY = ""
        srv._ctx_secret_key.set("")
        with self.assertRaises(ValueError) as ctx:
            srv._headers()
        self.assertIn("NEO_SECRET_KEY", str(ctx.exception))

    def test_http_mode_ctx_key(self):
        srv.NEO_SECRET_KEY = ""
        token = srv._ctx_secret_key.set("sk-v1-per-request")
        headers = srv._headers()
        self.assertEqual(headers["Authorization"], "Bearer sk-v1-per-request")
        srv._ctx_secret_key.reset(token)

    def test_authorization_header_format(self):
        srv.NEO_SECRET_KEY = "sk-v1-abc"
        h = srv._headers()
        self.assertTrue(h["Authorization"].startswith("Bearer "))


# ---------------------------------------------------------------------------
# 3. Thread ID persistence
# ---------------------------------------------------------------------------

class TestThreadIdPersistence(unittest.TestCase):
    def setUp(self):
        self._orig = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "active_thread_id")

    def tearDown(self):
        srv._THREAD_ID_FILE = self._orig

    def test_save_and_load(self):
        srv._save_thread_id("thread-abc123")
        self.assertEqual(srv._load_thread_id(), "thread-abc123")

    def test_load_missing_returns_empty(self):
        self.assertEqual(srv._load_thread_id(), "")

    def test_save_strips_whitespace_on_load(self):
        srv._save_thread_id("  thread-xyz  ")
        self.assertEqual(srv._load_thread_id(), "thread-xyz")

    def test_overwrite_previous(self):
        srv._save_thread_id("thread-1")
        srv._save_thread_id("thread-2")
        self.assertEqual(srv._load_thread_id(), "thread-2")

    def test_save_creates_parent_dirs(self):
        nested = os.path.join(self._tmpdir, "a", "b", "c", "thread_id")
        srv._THREAD_ID_FILE = nested
        srv._save_thread_id("thread-nested")
        self.assertEqual(srv._load_thread_id(), "thread-nested")


# ---------------------------------------------------------------------------
# 4. Thread ID resolution
# ---------------------------------------------------------------------------

class TestResolveThreadId(unittest.TestCase):
    def setUp(self):
        self._orig = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "active_thread_id")

    def tearDown(self):
        srv._THREAD_ID_FILE = self._orig

    def test_supplied_id_used_directly(self):
        tid, recovered = srv._resolve_thread_id({"thread_id": "thread-supplied"})
        self.assertEqual(tid, "thread-supplied")
        self.assertFalse(recovered)

    def test_unknown_string_falls_back_to_stored(self):
        srv._save_thread_id("thread-stored")
        tid, recovered = srv._resolve_thread_id({"thread_id": "unknown"})
        self.assertEqual(tid, "thread-stored")
        self.assertTrue(recovered)

    def test_missing_key_falls_back_to_stored(self):
        srv._save_thread_id("thread-stored")
        tid, recovered = srv._resolve_thread_id({})
        self.assertEqual(tid, "thread-stored")
        self.assertTrue(recovered)

    def test_no_stored_no_supplied_returns_empty(self):
        tid, recovered = srv._resolve_thread_id({})
        self.assertEqual(tid, "")
        self.assertFalse(recovered)

    def test_empty_string_falls_back(self):
        srv._save_thread_id("thread-fallback")
        tid, recovered = srv._resolve_thread_id({"thread_id": ""})
        self.assertEqual(tid, "thread-fallback")
        self.assertTrue(recovered)

    def test_whitespace_only_falls_back(self):
        srv._save_thread_id("thread-ws")
        tid, recovered = srv._resolve_thread_id({"thread_id": "   "})
        self.assertEqual(tid, "thread-ws")
        self.assertTrue(recovered)


# ---------------------------------------------------------------------------
# 5. Sandbox / deployment ID discovery
# ---------------------------------------------------------------------------

class TestDiscoverSandboxId(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._patch = patch("neo_mcp.server.os.path.expanduser",
                            side_effect=lambda p: p.replace("~", self._tmpdir))
        self._patch.start()
        os.makedirs(os.path.join(self._tmpdir, ".neo", "daemon"), exist_ok=True)
        self._daemon_dir = os.path.join(self._tmpdir, ".neo", "daemon")

    def tearDown(self):
        self._patch.stop()

    def _write_log(self, lines: list[str], filename="daemon.log"):
        path = os.path.join(self._daemon_dir, filename)
        with open(path, "w") as f:
            f.write("\n".join(lines))

    def _write_workspaces(self, data: dict):
        path = os.path.join(self._daemon_dir, "thread-workspaces.json")
        with open(path, "w") as f:
            json.dump(data, f)

    def test_reads_sandbox_id_from_daemon_log(self):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_log([f'{{"sandboxId": "{uid}"}}'])
        result = srv._discover_sandbox_id()
        self.assertEqual(result, uid)

    def test_takes_last_sandbox_id_from_log(self):
        uid1 = "11111111-1111-1111-1111-111111111111"
        uid2 = "22222222-2222-2222-2222-222222222222"
        self._write_log([
            f'{{"sandboxId": "{uid1}"}}',
            f'{{"sandboxId": "{uid2}"}}',
        ])
        result = srv._discover_sandbox_id()
        self.assertEqual(result, uid2)

    def test_does_not_read_workspaces_json(self):
        uid = "33333333-3333-3333-3333-333333333333"
        self._write_workspaces({uid: "/some/workspace"})
        result = srv._discover_sandbox_id()
        self.assertEqual(result, "")

    def test_returns_empty_when_no_files(self):
        result = srv._discover_sandbox_id()
        self.assertEqual(result, "")

    def test_ignores_invalid_uuid_format_in_log(self):
        self._write_log(['{"sandboxId": "not-a-valid-uuid"}'])
        result = srv._discover_sandbox_id()
        self.assertEqual(result, "")

    def test_daemon_log_parse_error_returns_empty_without_standalone(self):
        self._write_log(["THIS IS NOT JSON AT ALL"])
        self._write_workspaces({"44444444-4444-4444-4444-444444444444": "/workspace"})
        result = srv._discover_sandbox_id()
        self.assertEqual(result, "")

    def test_log_entry_wins_even_if_workspaces_file_exists(self):
        uid_log = "55555555-5555-5555-5555-555555555555"
        uid_ws = "66666666-6666-6666-6666-666666666666"
        self._write_log([f'{{"sandboxId": "{uid_log}"}}'])
        self._write_workspaces({uid_ws: "/workspace"})
        result = srv._discover_sandbox_id()
        self.assertEqual(result, uid_log)


# ---------------------------------------------------------------------------
# 5b. _vscode_daemon_deployment_id — daemon.log only, no standalone fallback
# ---------------------------------------------------------------------------

class TestVscodeDaemonDeploymentId(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._patch = patch("neo_mcp.server.os.path.expanduser",
                            side_effect=lambda p: p.replace("~", self._tmpdir))
        self._patch.start()
        os.makedirs(os.path.join(self._tmpdir, ".neo", "daemon"), exist_ok=True)
        self._daemon_dir = os.path.join(self._tmpdir, ".neo", "daemon")

    def tearDown(self):
        self._patch.stop()

    def _write_log(self, lines, filename="daemon.log"):
        path = os.path.join(self._daemon_dir, filename)
        with open(path, "w") as f:
            f.write("\n".join(lines))

    def test_returns_id_from_daemon_log(self):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_log([f'{{"sandboxId": "{uid}"}}'])
        self.assertEqual(srv._vscode_daemon_deployment_id(), uid)

    def test_returns_last_id_when_multiple_entries(self):
        uid1 = "11111111-1111-1111-1111-111111111111"
        uid2 = "22222222-2222-2222-2222-222222222222"
        self._write_log([f'{{"sandboxId": "{uid1}"}}', f'{{"sandboxId": "{uid2}"}}'])
        self.assertEqual(srv._vscode_daemon_deployment_id(), uid2)

    def test_falls_back_to_daemon_log_1(self):
        uid = "33333333-3333-3333-3333-333333333333"
        self._write_log([f'{{"sandboxId": "{uid}"}}'], filename="daemon.log.1")
        self.assertEqual(srv._vscode_daemon_deployment_id(), uid)

    def test_prefers_daemon_log_over_log_1(self):
        uid1 = "44444444-4444-4444-4444-444444444444"
        uid2 = "55555555-5555-5555-5555-555555555555"
        self._write_log([f'{{"sandboxId": "{uid1}"}}'], filename="daemon.log")
        self._write_log([f'{{"sandboxId": "{uid2}"}}'], filename="daemon.log.1")
        self.assertEqual(srv._vscode_daemon_deployment_id(), uid1)

    def test_returns_empty_when_no_log_files(self):
        self.assertEqual(srv._vscode_daemon_deployment_id(), "")

    def test_returns_empty_for_invalid_uuid_format(self):
        self._write_log(['{"sandboxId": "not-a-valid-uuid-here"}'])
        self.assertEqual(srv._vscode_daemon_deployment_id(), "")

    def test_does_not_fall_back_to_standalone_deployment_id(self):
        """Critical: unlike _discover_sandbox_id, must NOT read standalone_deployment_id."""
        uid = "66666666-6666-6666-6666-666666666666"
        standalone = os.path.join(self._daemon_dir, "standalone_deployment_id")
        with open(standalone, "w") as f:
            f.write(uid)
        # No daemon.log — should return empty, not the standalone file
        self.assertEqual(srv._vscode_daemon_deployment_id(), "")

    def test_does_not_fall_back_to_workspaces_json(self):
        """Critical: must NOT read thread-workspaces.json."""
        uid = "77777777-7777-7777-7777-777777777777"
        ws_path = os.path.join(self._daemon_dir, "thread-workspaces.json")
        with open(ws_path, "w") as f:
            json.dump({uid: "/workspace"}, f)
        self.assertEqual(srv._vscode_daemon_deployment_id(), "")

    def test_handles_mixed_plaintext_and_json_lines(self):
        uid = "88888888-8888-8888-8888-888888888888"
        self._write_log([
            "[2026-01-01] Daemon started",
            '{"sandboxId": "tooshort"}',
            f'{{"sandboxId": "{uid}"}}',
            "[2026-01-01] Polling started",
        ])
        self.assertEqual(srv._vscode_daemon_deployment_id(), uid)

    def test_empty_log_file_returns_empty(self):
        self._write_log([])
        self.assertEqual(srv._vscode_daemon_deployment_id(), "")

    def test_recovers_id_after_non_json_only_log(self):
        """Falls back to daemon.log.1 when daemon.log exists but has no sandboxId."""
        uid = "99999999-9999-9999-9999-999999999999"
        self._write_log(["[2026-01-01] No sandbox yet"], filename="daemon.log")
        self._write_log([f'{{"sandboxId": "{uid}"}}'], filename="daemon.log.1")
        self.assertEqual(srv._vscode_daemon_deployment_id(), uid)


class TestGetDeploymentId(unittest.TestCase):
    def setUp(self):
        self._orig = srv.NEO_DEPLOYMENT_ID
        self._orig_key = srv.NEO_SECRET_KEY

    def tearDown(self):
        srv.NEO_DEPLOYMENT_ID = self._orig
        srv.NEO_SECRET_KEY = self._orig_key

    def test_env_override_takes_priority(self):
        srv.NEO_DEPLOYMENT_ID = "env-override-id"
        result = srv._get_deployment_id()
        self.assertEqual(result, "env-override-id")

    def test_key_derived_when_env_not_set(self):
        srv.NEO_DEPLOYMENT_ID = ""
        srv.NEO_SECRET_KEY = "sk-v1-priority-test"
        result = srv._get_deployment_id()
        self.assertRegex(result, r"^[a-f0-9\-]{36}$")

    def test_returns_empty_when_no_header_env_or_key(self):
        srv.NEO_DEPLOYMENT_ID = ""
        srv.NEO_SECRET_KEY = ""
        result = srv._get_deployment_id()
        self.assertEqual(result, "")

    def test_returns_empty_when_nothing_found_and_no_key(self):
        """When no header, env var, files, AND no API key: return empty."""
        srv.NEO_DEPLOYMENT_ID = ""
        orig_key = srv.NEO_SECRET_KEY
        try:
            srv.NEO_SECRET_KEY = ""
            srv._ctx_secret_key.set("")
            result = srv._get_deployment_id()
            self.assertEqual(result, "")
        finally:
            srv.NEO_SECRET_KEY = orig_key

    def test_falls_back_to_key_derived_uuid(self):
        """When no header/env/files but API key is set, derive UUID from key."""
        srv.NEO_DEPLOYMENT_ID = ""
        orig_key = srv.NEO_SECRET_KEY
        try:
            srv.NEO_SECRET_KEY = "sk-v1-testkey"
            srv._ctx_secret_key.set("")
            result = srv._get_deployment_id()
            self.assertRegex(result, r"^[a-f0-9\-]{36}$")
            # Same key → same UUID on every call (deterministic)
            result2 = srv._get_deployment_id()
            self.assertEqual(result, result2)
            # Different key → different UUID
            srv.NEO_SECRET_KEY = "sk-v1-otherkey"
            result3 = srv._get_deployment_id()
            self.assertNotEqual(result, result3)
        finally:
            srv.NEO_SECRET_KEY = orig_key


class TestNpmDaemonRunning(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._patch = patch(
            "neo_mcp.server.os.path.expanduser",
            side_effect=lambda p: p.replace("~", self._tmpdir),
        )
        self._patch.start()
        os.makedirs(os.path.join(self._tmpdir, ".neo", "daemon"), exist_ok=True)
        self._daemon_dir = os.path.join(self._tmpdir, ".neo", "daemon")

    def tearDown(self):
        self._patch.stop()

    def _write_pid(self, name: str, pid: int):
        with open(os.path.join(self._daemon_dir, name), "w") as f:
            f.write(str(pid))

    def test_false_when_only_legacy_pid_exists(self):
        dep = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_pid("daemon_aaaaaaaa.pid", 1234)
        with patch("neo_mcp.server.os.kill", return_value=None):
            self.assertFalse(srv._npm_daemon_running(dep))

    def test_true_when_npm_and_deployment_pid_match(self):
        dep = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_pid("npm_daemon.pid", 4321)
        self._write_pid("daemon_aaaaaaaa.pid", 4321)
        with patch("neo_mcp.server.os.kill", return_value=None):
            self.assertTrue(srv._npm_daemon_running(dep))

    def test_false_when_npm_and_deployment_pid_differ(self):
        dep = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_pid("npm_daemon.pid", 1111)
        self._write_pid("daemon_aaaaaaaa.pid", 2222)
        with patch("neo_mcp.server.os.kill", return_value=None):
            self.assertFalse(srv._npm_daemon_running(dep))

    def test_stale_npm_pid_file_is_cleaned_up(self):
        self._write_pid("npm_daemon.pid", 9999)
        npm_pid = os.path.join(self._daemon_dir, "npm_daemon.pid")
        self.assertTrue(os.path.exists(npm_pid))
        with patch("neo_mcp.server.os.kill", side_effect=OSError("no such process")):
            self.assertFalse(srv._npm_daemon_running(""))
        self.assertFalse(os.path.exists(npm_pid))


# ---------------------------------------------------------------------------
# 6. Message pagination
# ---------------------------------------------------------------------------

class TestFetchMessagesPages(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_client(self, pages: list[dict]) -> AsyncMock:
        """Return a mock client whose .get() returns successive pages."""
        call_count = [0]

        async def mock_get(url, **kwargs):
            r = MagicMock()
            r.status_code = 200
            idx = min(call_count[0], len(pages) - 1)
            r.json.return_value = pages[call_count[0]] if call_count[0] < len(pages) else {"messages": [], "has_more": False}
            call_count[0] += 1
            return r

        c = AsyncMock()
        c.get = AsyncMock(side_effect=mock_get)
        return c

    def test_single_page_no_cap(self):
        msgs = [{"content": "hello", "created_at": 1}]
        client = self._make_client([{"messages": msgs, "has_more": False}])
        result, capped = self._run(srv._fetch_messages_pages(client, "tid-1"))
        self.assertEqual(result, msgs)
        self.assertFalse(capped)

    def test_multiple_pages(self):
        page1 = {"messages": [{"content": "a", "created_at": 1}], "has_more": True}
        page2 = {"messages": [{"content": "b", "created_at": 2}], "has_more": False}
        client = self._make_client([page1, page2])
        result, capped = self._run(srv._fetch_messages_pages(client, "tid-2"))
        self.assertEqual(len(result), 2)
        self.assertFalse(capped)

    def test_char_cap_triggers_capped_flag(self):
        big_msg = {"content": "x" * 90_000, "created_at": 1}
        client = self._make_client([{"messages": [big_msg], "has_more": False}])
        result, capped = self._run(srv._fetch_messages_pages(client, "tid-3"))
        self.assertTrue(capped)

    def test_empty_response(self):
        client = self._make_client([{"messages": [], "has_more": False}])
        result, capped = self._run(srv._fetch_messages_pages(client, "tid-4"))
        self.assertEqual(result, [])
        self.assertFalse(capped)

    def test_api_error_returns_partial(self):
        async def mock_get(url, **kwargs):
            r = MagicMock()
            r.status_code = 500
            return r

        c = AsyncMock()
        c.get = AsyncMock(side_effect=mock_get)
        result, capped = self._run(srv._fetch_messages_pages(c, "tid-5"))
        self.assertEqual(result, [])
        self.assertFalse(capped)


# ---------------------------------------------------------------------------
# 7. Background poller — adaptive delay and terminal states
# ---------------------------------------------------------------------------

class TestPollTaskBg(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_status_client(self, statuses: list[str], messages=None) -> AsyncMock:
        """Client that returns successive status values then COMPLETED."""
        call_count = [0]
        msgs = messages or []

        async def mock_get(url, **kwargs):
            r = MagicMock()
            r.status_code = 200
            if "thread-messages" in str(url):
                r.json.return_value = {"messages": msgs, "has_more": False}
            else:
                idx = min(call_count[0], len(statuses) - 1)
                r.json.return_value = {"status": statuses[call_count[0]], "current_plan": []}
                call_count[0] += 1
            return r

        c = AsyncMock()
        c.get = AsyncMock(side_effect=mock_get)
        return c

    def test_running_then_completed(self):
        statuses = ["RUNNING", "RUNNING", "COMPLETED"]
        client = self._make_status_client(statuses, messages=[{"content": "done", "sender": "neo"}])

        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            with patch("asyncio.sleep", new_callable=AsyncMock):
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=client)
                ctx.__aexit__ = AsyncMock(return_value=False)
                MockClient.return_value = ctx
                srv._active_polls.clear()
                self._run(srv._poll_task_bg("tid-poll-1"))

        state = srv._active_polls.get("tid-poll-1", {})
        self.assertEqual(state.get("status"), "COMPLETED")
        self.assertIsNotNone(state.get("messages"))

    def test_terminated_state_exits_loop(self):
        statuses = ["RUNNING", "TERMINATED"]
        client = self._make_status_client(statuses)

        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            with patch("asyncio.sleep", new_callable=AsyncMock):
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=client)
                ctx.__aexit__ = AsyncMock(return_value=False)
                MockClient.return_value = ctx
                srv._active_polls.clear()
                self._run(srv._poll_task_bg("tid-poll-2"))

        state = srv._active_polls.get("tid-poll-2", {})
        self.assertEqual(state.get("status"), "TERMINATED")

    def test_plan_stored_from_status_response(self):
        plan = [{"id": 1, "description": "Step 1", "status": "COMPLETED"}]
        call_count = [0]

        async def mock_get(url, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "status": "COMPLETED" if call_count[0] > 0 else "RUNNING",
                "current_plan": plan,
            }
            call_count[0] += 1
            return r

        client = AsyncMock()
        client.get = AsyncMock(side_effect=mock_get)

        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            with patch("asyncio.sleep", new_callable=AsyncMock):
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=client)
                ctx.__aexit__ = AsyncMock(return_value=False)
                MockClient.return_value = ctx
                srv._active_polls.clear()
                self._run(srv._poll_task_bg("tid-poll-3"))

        state = srv._active_polls.get("tid-poll-3", {})
        self.assertEqual(state.get("plan"), plan)


# ---------------------------------------------------------------------------
# 8. Tool: neo_submit_task
# ---------------------------------------------------------------------------

class TestNeoSubmitTask(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        self._orig_transport = srv.NEO_TRANSPORT
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = srv._active_polls.copy()
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv.NEO_TRANSPORT = self._orig_transport
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_submit_returns_thread_id(self):
        resp_ok = make_response(200, {"thread_id": "tid-submit-1"})

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-123"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, client = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "train a model"})

        txt = text_of(result)
        self.assertIn("tid-submit-1", txt)

    def test_submit_400_returns_error(self):
        resp_400 = make_response(400, {"detail": "No healthy deployments"})

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-123"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, client = make_async_client({"DEFAULT": resp_400})
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "train a model"})

        txt = text_of(result)
        self.assertIn("DAEMON_NOT_RUNNING", txt)
        self.assertIn("neo-mcp-daemon", txt)

    def test_submit_http_mode_does_not_autostart_daemon(self):
        """HTTP transport must not attempt daemon auto-start from server process."""
        resp_ok = make_response(200, {"thread_id": "tid-http-auto"})
        srv.NEO_TRANSPORT = "http"
        mock_ensure = AsyncMock(return_value=True)

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-http"), \
             patch("neo_mcp.server._ensure_local_daemon", mock_ensure), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, _ = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "train a model"})

        self.assertIn("tid-http-auto", text_of(result))
        mock_ensure.assert_not_awaited()

    def test_submit_400_retries_once_same_deployment_id(self):
        """Daemon-only mode retries once after ensure_local_daemon."""
        dep_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        captured_ids: list[str] = []

        async def mock_post(url, **kwargs):
            body = kwargs.get("json", {})
            captured_ids.append(body.get("deployment_id"))
            # 1st call -> 400, 2nd call (retry) -> 200
            if len(captured_ids) == 1:
                return make_response(400, {"detail": "No healthy deployments"})
            return make_response(200, {"thread_id": "tid-retry"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        dep_token = srv._ctx_deployment_id.set("")
        try:
            with patch("neo_mcp.server._get_deployment_id", return_value=dep_id), \
             patch("neo_mcp.server._ensure_local_daemon", new_callable=AsyncMock, return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient", return_value=ctx), \
             patch("asyncio.create_task"):
                result = call_tool("neo_submit_task", {"description": "task"})
        finally:
            srv._ctx_deployment_id.reset(dep_token)

        self.assertIn("tid-retry", text_of(result))
        self.assertEqual(captured_ids, [dep_id, dep_id])

    def test_submit_401_returns_invalid_key_error(self):
        resp_401 = make_response(401, {"detail": "Unauthorized"})

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-123"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, _ = make_async_client({"DEFAULT": resp_401})
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "train a model"})

        txt = text_of(result)
        self.assertIn("Invalid API key", txt)

    def test_submit_saves_thread_id(self):
        resp_ok = make_response(200, {"thread_id": "tid-persist"})

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-123"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, _ = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx
            call_tool("neo_submit_task", {"description": "task"})

        self.assertEqual(srv._load_thread_id(), "tid-persist")

    def test_submit_no_deployment_id_still_works(self):
        """When no deployment found, key-derived UUID is used — Neo returns 200."""
        resp_ok = make_response(200, {"thread_id": "tid-no-dep"})

        with patch("neo_mcp.server._get_deployment_id", return_value=""), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, mock_client = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "task"})

        txt = text_of(result)
        self.assertIn("tid-no-dep", txt)

    def test_submit_includes_working_directory_prefix(self):
        """Task description should include the working directory context."""
        captured_body = {}
        resp_ok = make_response(200, {"thread_id": "tid-prefix"})

        async def mock_post(url, **kwargs):
            captured_body.update(kwargs.get("json", {}))
            return resp_ok

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-abc"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient", return_value=ctx), \
             patch("asyncio.create_task"):
            call_tool("neo_submit_task", {"description": "train model"})

        self.assertIn("Working directory", captured_body.get("message", ""))
        self.assertIn("train model", captured_body.get("message", ""))

    def test_submit_network_error_returns_message(self):
        import httpx as _httpx

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-123"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("refused"))
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=mock_client)
            ctx.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "task"})

        txt = text_of(result)
        self.assertIn("Network error", txt)

    def test_submit_wait_for_completion_returns_output(self):
        resp_ok = make_response(200, {"thread_id": "tid-wait"})

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-123"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"), \
             patch("asyncio.sleep", new_callable=AsyncMock):

            ctx, _ = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx

            # Pre-populate the poll state so wait loop exits on first check
            srv._active_polls["tid-wait"] = {
                "status": "COMPLETED",
                "messages": [{"sender": "neo", "content": "Model trained!"}],
                "capped": False,
                "plan": [],
            }
            result = call_tool("neo_submit_task", {
                "description": "task",
                "wait_for_completion": True,
            })

        txt = text_of(result)
        self.assertIn("COMPLETED", txt)
        self.assertIn("Model trained!", txt)

    # -- daemon-only auto-start routing tests --------------------------------

    def test_registerable_local_daemon_skips_autostart(self):
        """Reachable daemon registration should satisfy readiness without restarts."""
        mock_register = AsyncMock(return_value=True)
        mock_npm_start = AsyncMock()
        mock_py_start = AsyncMock()

        with patch("neo_mcp.server._register_with_daemon", mock_register), \
             patch("neo_mcp.server._npm_daemon_running", return_value=False), \
             patch("neo_mcp.server._python_daemon_running", return_value=False), \
             patch("neo_mcp.server._auto_start_npm_daemon", mock_npm_start), \
             patch("neo_mcp.server._auto_start_python_daemon", mock_py_start):
            ready = self._run(srv._ensure_local_daemon("sk-v1-test", "dep-xyz", "/tmp/ws"))

        self.assertTrue(ready)
        mock_register.assert_called_once_with("dep-xyz", "sk-v1-test", "/tmp/ws")
        mock_npm_start.assert_not_called()
        mock_py_start.assert_not_called()

    def test_no_npm_daemon_autostart_triggered(self):
        """When npm daemon is not running, auto-start must be called."""
        resp_ok = make_response(200, {"thread_id": "tid-no-daemon"})
        mock_daemon = AsyncMock(return_value=True)

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-xyz"), \
             patch("neo_mcp.server._register_with_daemon", new_callable=AsyncMock, return_value=False), \
             patch("neo_mcp.server._npm_daemon_running", return_value=False), \
             patch("neo_mcp.server._auto_start_npm_daemon", mock_daemon), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, _ = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "train model"})

        mock_daemon.assert_called_once()
        self.assertIn("tid-no-daemon", text_of(result))

    def test_python_daemon_fallback_when_npm_start_fails(self):
        """If npm daemon cannot start, Python daemon fallback is attempted."""
        resp_ok = make_response(200, {"thread_id": "tid-py-fallback"})
        mock_npm_start = AsyncMock(return_value=False)
        mock_py_start = AsyncMock(return_value=True)

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-xyz"), \
             patch("neo_mcp.server._register_with_daemon", new_callable=AsyncMock, return_value=False), \
             patch("neo_mcp.server._npm_daemon_running", return_value=False), \
             patch("neo_mcp.server._python_daemon_running", return_value=False), \
             patch("neo_mcp.server._auto_start_npm_daemon", mock_npm_start), \
             patch("neo_mcp.server._auto_start_python_daemon", mock_py_start), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, _ = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx
            result = call_tool("neo_submit_task", {"description": "train model"})

        mock_npm_start.assert_called_once()
        mock_py_start.assert_called_once()
        self.assertIn("tid-py-fallback", text_of(result))

    def test_npm_daemon_already_running_no_restart(self):
        """If daemon already running for deployment, skip auto-start."""
        resp_ok = make_response(200, {"thread_id": "tid-already-running"})
        mock_daemon = AsyncMock()

        with patch("neo_mcp.server._get_deployment_id", return_value="dep-xyz"), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server._auto_start_npm_daemon", mock_daemon), \
             patch("neo_mcp.server.httpx.AsyncClient") as MockClient, \
             patch("asyncio.create_task"):
            ctx, _ = make_async_client({"DEFAULT": resp_ok})
            MockClient.return_value = ctx
            call_tool("neo_submit_task", {"description": "train model"})

        mock_daemon.assert_not_called()
    def test_deployment_id_sent_in_submit_body(self):
        """Resolved deployment_id must be forwarded to init-chat-direct."""
        captured = {}
        resp_ok = make_response(200, {"thread_id": "tid-body"})
        dep_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"

        async def mock_post(url, **kwargs):
            captured.update(kwargs.get("json", {}))
            return resp_ok

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("neo_mcp.server._get_deployment_id", return_value=dep_id), \
             patch("neo_mcp.server._npm_daemon_running", return_value=False), \
             patch("neo_mcp.server._auto_start_npm_daemon", new_callable=AsyncMock, return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient", return_value=ctx), \
             patch("asyncio.create_task"):
            call_tool("neo_submit_task", {"description": "task"})

        self.assertEqual(captured.get("deployment_id"), dep_id)
        self.assertEqual(captured.get("deployment_type"), "vscode")

    def test_local_daemon_id_used_in_stdio_when_no_existing_id(self):
        """In stdio mode, submit remaps key-derived ID to local-daemon ID."""
        captured = {}
        resp_ok = make_response(200, {"thread_id": "tid-key-derived"})
        key_uuid = "cccccccc-dddd-eeee-ffff-aaaaaaaaaaaa"

        async def mock_post(url, **kwargs):
            captured.update(kwargs.get("json", {}))
            return resp_ok

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("neo_mcp.server._get_deployment_id", return_value=""), \
             patch("neo_mcp.server._derive_deployment_id", return_value=key_uuid), \
             patch("neo_mcp.server._npm_daemon_running", return_value=True), \
             patch("neo_mcp.server.httpx.AsyncClient", return_value=ctx), \
             patch("asyncio.create_task"):
            call_tool("neo_submit_task", {"description": "task"})

        expected = srv._derive_local_daemon_deployment_id(srv.NEO_SECRET_KEY)
        self.assertEqual(captured.get("deployment_id"), expected)


# ---------------------------------------------------------------------------
# 9. Tool: neo_task_status
# ---------------------------------------------------------------------------

class TestNeoTaskStatus(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_returns_status_from_cache(self):
        srv._active_polls["tid-status-1"] = {
            "status": "RUNNING", "messages": None, "capped": False, "plan": []
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_task_status", {"thread_id": "tid-status-1"})

        txt = text_of(result)
        self.assertIn("RUNNING", txt)
        self.assertIn("tid-status-1", txt)

    def test_api_fallback_when_no_cache(self):
        resp = make_response(200, {"status": "COMPLETED", "current_plan": []})
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": resp})
            MockClient.return_value = ctx
            result = call_tool("neo_task_status", {"thread_id": "tid-status-2"})

        txt = text_of(result)
        self.assertIn("COMPLETED", txt)

    def test_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_task_status", {})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)

    def test_status_with_plan_renders_steps(self):
        plan = [
            {"id": 1, "description": "Load data", "status": "COMPLETED", "result_summary": "1000 rows"},
            {"id": 2, "description": "Train model", "status": "RUNNING", "result_summary": ""},
        ]
        srv._active_polls["tid-plan"] = {
            "status": "RUNNING", "messages": None, "capped": False, "plan": plan
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_task_status", {"thread_id": "tid-plan"})

        txt = text_of(result)
        self.assertIn("Load data", txt)
        self.assertIn("Train model", txt)
        self.assertIn("1000 rows", txt)

    def test_recovered_thread_id_noted_in_output(self):
        srv._save_thread_id("tid-recovered")
        srv._active_polls["tid-recovered"] = {
            "status": "RUNNING", "messages": None, "capped": False, "plan": []
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_task_status", {})

        txt = text_of(result)
        self.assertIn("tid-recovered", txt)

    def test_api_401_returns_error_message(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(401)})
            MockClient.return_value = ctx
            result = call_tool("neo_task_status", {"thread_id": "tid-unauth"})

        txt = text_of(result)
        self.assertIn("Invalid API key", txt)

    def test_status_hints_shown_for_each_state(self):
        for status, expected_hint in [
            ("WAITING_FOR_FEEDBACK", "neo_send_feedback"),
            ("PAUSED", "neo_resume_task"),
            ("COMPLETED", "neo_get_messages"),
            ("TERMINATED", "error"),
        ]:
            srv._active_polls[f"tid-hint-{status}"] = {
                "status": status, "messages": None, "capped": False, "plan": []
            }
            with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
                ctx, _ = make_async_client({})
                MockClient.return_value = ctx
                result = call_tool("neo_task_status", {"thread_id": f"tid-hint-{status}"})
            txt = text_of(result).lower()
            self.assertIn(expected_hint.lower(), txt,
                          f"Expected hint for {status} to mention '{expected_hint}'")


# ---------------------------------------------------------------------------
# 10. Tool: neo_task_plan
# ---------------------------------------------------------------------------

class TestNeoTaskPlan(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_plan_from_cache(self):
        plan = [
            {"id": 1, "description": "Load data", "status": "COMPLETED",
             "result_summary": "done", "current_activity": []},
            {"id": 2, "description": "Train model", "status": "RUNNING",
             "result_summary": "", "current_activity": ["epoch 3/10"]},
        ]
        srv._active_polls["tid-p1"] = {"status": "RUNNING", "plan": plan, "messages": None, "capped": False}
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_task_plan", {"thread_id": "tid-p1"})

        txt = text_of(result)
        self.assertIn("Load data", txt)
        self.assertIn("Train model", txt)
        self.assertIn("epoch 3/10", txt)

    def test_plan_from_api_when_no_cache(self):
        plan = [{"id": 1, "description": "Step A", "status": "RUNNING",
                 "result_summary": "", "current_activity": []}]
        resp = make_response(200, {"status": "RUNNING", "current_plan": plan})
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": resp})
            MockClient.return_value = ctx
            result = call_tool("neo_task_plan", {"thread_id": "tid-p2"})

        txt = text_of(result)
        self.assertIn("Step A", txt)

    def test_no_plan_yet_returns_message(self):
        srv._active_polls["tid-p3"] = {"status": "RUNNING", "plan": [], "messages": None, "capped": False}
        resp = make_response(200, {"status": "RUNNING", "current_plan": []})
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": resp})
            MockClient.return_value = ctx
            result = call_tool("neo_task_plan", {"thread_id": "tid-p3"})

        txt = text_of(result)
        self.assertIn("No plan available", txt)

    def test_plan_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_task_plan", {})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)

    def test_plan_icons_rendered(self):
        plan = [
            {"id": 1, "description": "A", "status": "COMPLETED", "result_summary": "", "current_activity": []},
            {"id": 2, "description": "B", "status": "RUNNING", "result_summary": "", "current_activity": []},
            {"id": 3, "description": "C", "status": "FAILED", "result_summary": "", "current_activity": []},
            {"id": 4, "description": "D", "status": "PENDING", "result_summary": "", "current_activity": []},
        ]
        srv._active_polls["tid-icons"] = {"status": "RUNNING", "plan": plan, "messages": None, "capped": False}
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_task_plan", {"thread_id": "tid-icons"})

        txt = text_of(result)
        self.assertIn("✅", txt)
        self.assertIn("⏳", txt)
        self.assertIn("❌", txt)
        self.assertIn("⬜", txt)


# ---------------------------------------------------------------------------
# 11. Tool: neo_get_messages
# ---------------------------------------------------------------------------

class TestNeoGetMessages(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_returns_cached_messages(self):
        srv._active_polls["tid-m1"] = {
            "status": "COMPLETED",
            "messages": [
                {"sender": "user", "content": "train model"},
                {"sender": "neo", "content": "Done! AUC 0.93"},
            ],
            "capped": False,
            "plan": [],
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_messages", {"thread_id": "tid-m1"})

        txt = text_of(result)
        self.assertIn("train model", txt)
        self.assertIn("AUC 0.93", txt)
        self.assertIn("USER", txt)
        self.assertIn("NEO", txt)

    def test_fetches_from_api_when_not_cached(self):
        msgs = [{"sender": "neo", "content": "Result: 0.95"}]
        resp = make_response(200, {"messages": msgs, "has_more": False})
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": resp})
            MockClient.return_value = ctx
            result = call_tool("neo_get_messages", {"thread_id": "tid-m2"})

        txt = text_of(result)
        self.assertIn("Result: 0.95", txt)

    def test_capped_output_includes_truncation_notice(self):
        srv._active_polls["tid-m3"] = {
            "status": "COMPLETED",
            "messages": [{"sender": "neo", "content": "lots of output"}],
            "capped": True,
            "plan": [],
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_messages", {"thread_id": "tid-m3"})

        txt = text_of(result)
        self.assertIn("truncated", txt.lower())

    def test_empty_messages_returns_no_messages(self):
        srv._active_polls["tid-m4"] = {
            "status": "COMPLETED", "messages": [], "capped": False, "plan": []
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_messages", {"thread_id": "tid-m4"})

        txt = text_of(result)
        self.assertIn("No messages", txt)

    def test_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_messages", {})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)


# ---------------------------------------------------------------------------
# 12. Tool: neo_get_files
# ---------------------------------------------------------------------------

class TestNeoGetFiles(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._orig_ws_file = srv._THREAD_WORKSPACES_FILE
        self._orig_cwd = srv._server_cwd
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")
        # Point workspace file into tmp so tests don't touch ~/.neo
        self._ws_file = os.path.join(self._tmpdir, "thread-workspaces.json")
        srv._THREAD_WORKSPACES_FILE = self._ws_file

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid
        srv._THREAD_WORKSPACES_FILE = self._orig_ws_file
        srv._server_cwd = self._orig_cwd

    def test_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_files", {})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)

    def test_no_files_in_empty_workspace(self):
        # Workspace mapped to empty dir — should report no files
        workspace = tempfile.mkdtemp()
        with open(self._ws_file, "w") as f:
            json.dump({"tid-f2": workspace}, f)

        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_files", {"thread_id": "tid-f2"})

        txt = text_of(result)
        self.assertIn("No files", txt)

    def test_files_returned_with_contents(self):
        # Create a workspace with two files written by the daemon
        workspace = tempfile.mkdtemp()
        with open(os.path.join(workspace, "model.py"), "w") as f:
            f.write("# model code\nprint('hello')\n")
        with open(os.path.join(workspace, "README.md"), "w") as f:
            f.write("# Project\nSome description.\n")
        with open(self._ws_file, "w") as f:
            json.dump({"tid-f3": workspace}, f)

        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_files", {"thread_id": "tid-f3"})

        txt = text_of(result)
        self.assertIn("model.py", txt)
        self.assertIn("# model code", txt)
        self.assertIn("README.md", txt)

    def test_no_workspace_map_returns_error(self):
        # No workspaces file — must fail fast (no server-side cwd fallback)
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_get_files", {"thread_id": "tid-f4"})

        txt = text_of(result)
        self.assertIn("No local workspace mapping found", txt)
        self.assertIn("Refusing server-side filesystem fallback", txt)

    def test_http_mode_returns_local_verification_instructions(self):
        # In HTTP mode, server must not read local files from hosted environment.
        workspace = tempfile.mkdtemp()
        with open(self._ws_file, "w") as f:
            json.dump({"tid-http-files": workspace}, f)
        orig_transport = srv.NEO_TRANSPORT
        try:
            srv.NEO_TRANSPORT = "http"
            with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
                ctx, _ = make_async_client({})
                MockClient.return_value = ctx
                result = call_tool("neo_get_files", {"thread_id": "tid-http-files"})
        finally:
            srv.NEO_TRANSPORT = orig_transport

        txt = text_of(result)
        self.assertIn("HTTP_REMOTE_MODE_NO_LOCAL_FS", txt)
        self.assertIn("Verify files on the user machine", txt)


# ---------------------------------------------------------------------------
# 13. Tool: neo_send_feedback
# ---------------------------------------------------------------------------

class TestNeoSendFeedback(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_feedback_sent_successfully(self):
        srv._active_polls["tid-fb1"] = {
            "status": "WAITING_FOR_FEEDBACK", "messages": None, "capped": False, "plan": []
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(200, {})})
            MockClient.return_value = ctx
            result = call_tool("neo_send_feedback", {
                "thread_id": "tid-fb1", "message": "Yes, use XGBoost"
            })

        txt = text_of(result)
        self.assertIn("Feedback sent", txt)

    def test_feedback_updates_cache_to_running(self):
        srv._active_polls["tid-fb2"] = {
            "status": "WAITING_FOR_FEEDBACK", "messages": None, "capped": False, "plan": []
        }
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(200, {})})
            MockClient.return_value = ctx
            call_tool("neo_send_feedback", {"thread_id": "tid-fb2", "message": "go"})

        self.assertEqual(srv._active_polls["tid-fb2"]["status"], "RUNNING")

    def test_feedback_error_returns_message(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(404)})
            MockClient.return_value = ctx
            result = call_tool("neo_send_feedback", {
                "thread_id": "tid-fb3", "message": "hello"
            })

        txt = text_of(result)
        self.assertIn("Thread or user not found", txt)

    def test_feedback_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_send_feedback", {"message": "hello"})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)


# ---------------------------------------------------------------------------
# 14. Tool: neo_pause_task
# ---------------------------------------------------------------------------

class TestNeoPauseTask(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_pause_success(self):
        srv._active_polls["tid-ps1"] = {"status": "RUNNING", "messages": None, "capped": False, "plan": []}
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(200, {})})
            MockClient.return_value = ctx
            result = call_tool("neo_pause_task", {"thread_id": "tid-ps1"})

        txt = text_of(result)
        self.assertIn("paused", txt.lower())
        self.assertEqual(srv._active_polls["tid-ps1"]["status"], "PAUSED")

    def test_pause_error_returns_message(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(404)})
            MockClient.return_value = ctx
            result = call_tool("neo_pause_task", {"thread_id": "tid-ps2"})

        txt = text_of(result)
        self.assertIn("not found", txt.lower())

    def test_pause_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_pause_task", {})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)


# ---------------------------------------------------------------------------
# 15. Tool: neo_resume_task
# ---------------------------------------------------------------------------

class TestNeoResumeTask(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_resume_success(self):
        srv._active_polls["tid-rs1"] = {"status": "PAUSED", "messages": None, "capped": False, "plan": []}
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(200, {})})
            MockClient.return_value = ctx
            result = call_tool("neo_resume_task", {"thread_id": "tid-rs1"})

        txt = text_of(result)
        self.assertIn("resumed", txt.lower())
        self.assertEqual(srv._active_polls["tid-rs1"]["status"], "RUNNING")

    def test_resume_error_returns_message(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(401)})
            MockClient.return_value = ctx
            result = call_tool("neo_resume_task", {"thread_id": "tid-rs2"})

        txt = text_of(result)
        self.assertIn("Invalid API key", txt)

    def test_resume_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_resume_task", {})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)


# ---------------------------------------------------------------------------
# 16. Tool: neo_stop_task
# ---------------------------------------------------------------------------

class TestNeoStopTask(unittest.TestCase):
    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = "sk-v1-test"
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_stop_success_clears_cache(self):
        srv._active_polls["tid-st1"] = {"status": "RUNNING", "messages": None, "capped": False, "plan": []}
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(200, {})})
            MockClient.return_value = ctx
            result = call_tool("neo_stop_task", {"thread_id": "tid-st1"})

        txt = text_of(result)
        self.assertIn("stopped", txt.lower())
        self.assertNotIn("tid-st1", srv._active_polls)

    def test_stop_error_returns_message(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({"DEFAULT": make_response(404)})
            MockClient.return_value = ctx
            result = call_tool("neo_stop_task", {"thread_id": "tid-st2"})

        txt = text_of(result)
        self.assertIn("not found", txt.lower())

    def test_stop_no_thread_id_returns_error(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_stop_task", {})

        txt = text_of(result)
        self.assertIn("No thread_id", txt)

    def test_stop_with_delete_artifacts_flag(self):
        captured = {}

        async def mock_delete(url, **kwargs):
            captured["params"] = kwargs.get("params", {})
            return make_response(200, {})

        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=mock_delete)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("neo_mcp.server.httpx.AsyncClient", return_value=ctx):
            call_tool("neo_stop_task", {
                "thread_id": "tid-st3",
                "delete_remote_artifacts": True,
            })

        self.assertIn("delete_remote_artifacts", captured.get("params", {}))


# ---------------------------------------------------------------------------
# 17. READ_ONLY mode
# ---------------------------------------------------------------------------

class TestReadOnlyMode(unittest.TestCase):
    def setUp(self):
        self._orig = srv.NEO_READ_ONLY

    def tearDown(self):
        srv.NEO_READ_ONLY = self._orig

    def _get_tool_names(self) -> list[str]:
        req = mcp_types.ListToolsRequest(method="tools/list", params=None)
        handler = srv.app.request_handlers[mcp_types.ListToolsRequest]
        result = asyncio.get_event_loop().run_until_complete(handler(req))
        return [t.name for t in result.root.tools]

    def test_all_tools_present_when_not_read_only(self):
        srv.NEO_READ_ONLY = False
        names = self._get_tool_names()
        for tool in ["neo_submit_task", "neo_stop_task", "neo_pause_task",
                     "neo_resume_task", "neo_send_feedback",
                     "neo_task_status", "neo_task_plan", "neo_get_messages", "neo_get_files"]:
            self.assertIn(tool, names, f"{tool} should be present in normal mode")

    def test_write_tools_absent_in_read_only(self):
        srv.NEO_READ_ONLY = True
        names = self._get_tool_names()
        for tool in ["neo_submit_task", "neo_stop_task", "neo_pause_task", "neo_resume_task", "neo_send_feedback"]:
            self.assertNotIn(tool, names, f"{tool} should NOT be present in read-only mode")

    def test_read_tools_present_in_read_only(self):
        srv.NEO_READ_ONLY = True
        names = self._get_tool_names()
        for tool in ["neo_task_status", "neo_task_plan", "neo_get_messages", "neo_get_files"]:
            self.assertIn(tool, names, f"{tool} should be present in read-only mode")


# ---------------------------------------------------------------------------
# 18. HTTP Bearer token extraction
# ---------------------------------------------------------------------------

class TestBearerTokenExtraction(unittest.TestCase):
    """Test the HTTP handler's Bearer extraction logic directly."""

    def _extract(self, header_value: str) -> str:
        """Replicate the extraction logic from _run_http → handle_mcp."""
        secret_key = header_value
        if secret_key.lower().startswith("bearer "):
            secret_key = secret_key[7:].strip()
        return secret_key

    def test_clean_bearer(self):
        self.assertEqual(self._extract("Bearer sk-v1-abc"), "sk-v1-abc")

    def test_double_space_bearer(self):
        self.assertEqual(self._extract("Bearer  sk-v1-abc"), "sk-v1-abc")

    def test_trailing_space(self):
        self.assertEqual(self._extract("Bearer sk-v1-abc  "), "sk-v1-abc")

    def test_lowercase_bearer(self):
        self.assertEqual(self._extract("bearer sk-v1-abc"), "sk-v1-abc")

    def test_mixed_case_bearer(self):
        self.assertEqual(self._extract("BEARER sk-v1-abc"), "sk-v1-abc")

    def test_no_bearer_prefix(self):
        self.assertEqual(self._extract("sk-v1-abc"), "sk-v1-abc")

    def test_empty_string(self):
        self.assertEqual(self._extract(""), "")


# ---------------------------------------------------------------------------
# 19. Unknown tool name
# ---------------------------------------------------------------------------

class TestUnknownTool(unittest.TestCase):
    def setUp(self):
        srv.NEO_SECRET_KEY = "sk-v1-test"

    def test_unknown_tool_returns_error_text(self):
        with patch("neo_mcp.server.httpx.AsyncClient") as MockClient:
            ctx, _ = make_async_client({})
            MockClient.return_value = ctx
            result = call_tool("neo_does_not_exist", {})

        txt = text_of(result)
        self.assertIn("Unknown tool", txt)


# ---------------------------------------------------------------------------
# 20. Integration smoke tests (skipped without real key)
# ---------------------------------------------------------------------------

REAL_KEY = os.environ.get("NEO_SECRET_KEY_REAL", "")

@unittest.skipUnless(REAL_KEY and REAL_KEY.startswith("sk-v1-"), "Set NEO_SECRET_KEY_REAL to run integration tests")
class TestIntegration(unittest.TestCase):
    """Smoke tests against the real Neo backend.

    Run with:
        NEO_SECRET_KEY_REAL=sk-v1-... python -m pytest tests/ -v -k Integration
    """

    def setUp(self):
        self._orig_key = srv.NEO_SECRET_KEY
        srv.NEO_SECRET_KEY = REAL_KEY
        self._orig_polls = dict(srv._active_polls)
        srv._active_polls.clear()
        self._orig_tid = srv._THREAD_ID_FILE
        self._tmpdir = tempfile.mkdtemp()
        srv._THREAD_ID_FILE = os.path.join(self._tmpdir, "tid")

    def tearDown(self):
        srv.NEO_SECRET_KEY = self._orig_key
        srv._active_polls.clear()
        srv._active_polls.update(self._orig_polls)
        srv._THREAD_ID_FILE = self._orig_tid

    def test_auth_valid(self):
        """Submitting a trivial task should not return 401."""
        result = call_tool("neo_submit_task", {"description": "echo hello world"})
        txt = text_of(result)
        self.assertNotIn("Invalid API key", txt)
        self.assertNotIn("401", txt)

    def test_submit_returns_thread_id(self):
        result = call_tool("neo_submit_task", {"description": "echo hello"})
        txt = text_of(result)
        # Should have a thread_id or a deployment-related 400
        has_thread = "thread_id:" in txt
        has_deploy_err = "deployment" in txt.lower()
        self.assertTrue(has_thread or has_deploy_err,
                        f"Expected thread_id or deployment error, got: {txt[:200]}")

    def test_task_status_with_valid_thread(self):
        """Submit then immediately check status — should not crash."""
        submit = call_tool("neo_submit_task", {"description": "print('hi')"})
        submit_txt = text_of(submit)
        if "thread_id:" not in submit_txt:
            self.skipTest("No thread_id returned (no deployment available)")

        # Extract thread_id
        for part in submit_txt.split():
            if part.startswith("tid-") or len(part) > 20:
                tid = part.rstrip(".\n")
                if "-" in tid and len(tid) > 10:
                    break

        status = call_tool("neo_task_status", {"thread_id": tid})
        status_txt = text_of(status)
        valid_statuses = ["RUNNING", "COMPLETED", "TERMINATED", "PAUSED", "WAITING"]
        self.assertTrue(
            any(s in status_txt for s in valid_statuses),
            f"Expected a valid status, got: {status_txt[:200]}"
        )


# ---------------------------------------------------------------------------
# 21. HTTP transport — session management and routing
# ---------------------------------------------------------------------------

class TestHttpTransport(unittest.IsolatedAsyncioTestCase):
    """Tests for _build_http_app(): session creation, auth, tool availability.

    Uses httpx.AsyncClient with ASGITransport — no network, no uvicorn.
    Each test builds a fresh app instance (fresh session store).
    """

    _INIT_PAYLOAD = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        },
    }
    _INITIALIZED_PAYLOAD = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    _TOOLS_LIST_PAYLOAD = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    _MCP_HEADERS = {
        "Authorization": "Bearer sk-v1-test",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def _make_client(self):
        """Return an httpx.AsyncClient wired to a fresh ASGI app instance."""
        import httpx
        app = srv._build_http_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    @staticmethod
    def _parse_sse(content: bytes) -> list[dict]:
        """Extract JSON objects from SSE data lines."""
        results = []
        for line in content.decode().splitlines():
            if line.startswith("data: "):
                try:
                    results.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        return results

    # --- Auth ---

    async def test_no_auth_returns_401(self):
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
        self.assertEqual(resp.status_code, 401)

    async def test_empty_bearer_returns_401(self):
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers={
                    "Authorization": "Bearer ",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        self.assertEqual(resp.status_code, 401)

    async def test_401_includes_www_authenticate_header(self):
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
        self.assertIn("www-authenticate", resp.headers)
        self.assertIn("Bearer", resp.headers["www-authenticate"])

    async def test_401_body_is_json(self):
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertIn("error", body)

    async def test_double_space_bearer_is_accepted(self):
        """Bearer tokens with extra leading space (common copy-paste mistake) must still work."""
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers={
                    "Authorization": "Bearer  sk-v1-test",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        # Must NOT be 401 — extra space is stripped
        self.assertNotEqual(resp.status_code, 401)

    # --- Session establishment ---

    async def test_initialize_returns_200(self):
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers=self._MCP_HEADERS,
            )
        self.assertEqual(resp.status_code, 200)

    async def test_initialize_returns_mcp_session_id_header(self):
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers=self._MCP_HEADERS,
            )
        self.assertIn("mcp-session-id", resp.headers)
        session_id = resp.headers["mcp-session-id"]
        self.assertTrue(len(session_id) > 0)

    async def test_initialize_response_contains_server_info(self):
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers=self._MCP_HEADERS,
            )
        messages = self._parse_sse(resp.content)
        self.assertTrue(len(messages) > 0, "Expected at least one SSE message")
        init_result = messages[0]
        self.assertIn("result", init_result)
        self.assertIn("serverInfo", init_result["result"])
        self.assertEqual(init_result["result"]["serverInfo"]["name"], "neo-mcp")

    # --- Tool listing after initialization ---

    async def _do_handshake(self, client) -> str:
        """Run the full MCP handshake and return the session ID."""
        init_resp = await client.post(
            "/mcp",
            content=json.dumps(self._INIT_PAYLOAD),
            headers=self._MCP_HEADERS,
        )
        self.assertEqual(init_resp.status_code, 200)
        session_id = init_resp.headers["mcp-session-id"]

        await client.post(
            "/mcp",
            content=json.dumps(self._INITIALIZED_PAYLOAD),
            headers={**self._MCP_HEADERS, "mcp-session-id": session_id},
        )
        return session_id

    async def test_tools_list_returns_200(self):
        async with self._make_client() as client:
            session_id = await self._do_handshake(client)
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._TOOLS_LIST_PAYLOAD),
                headers={**self._MCP_HEADERS, "mcp-session-id": session_id},
            )
        self.assertEqual(resp.status_code, 200)

    async def test_tools_list_returns_all_nine_tools(self):
        async with self._make_client() as client:
            session_id = await self._do_handshake(client)
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._TOOLS_LIST_PAYLOAD),
                headers={**self._MCP_HEADERS, "mcp-session-id": session_id},
            )
        messages = self._parse_sse(resp.content)
        tools_result = next(
            (m for m in messages if "result" in m and "tools" in m.get("result", {})),
            None,
        )
        self.assertIsNotNone(tools_result, f"No tools/list result in SSE: {messages}")
        tool_names = [t["name"] for t in tools_result["result"]["tools"]]
        expected = [
            "neo_submit_task", "neo_task_status", "neo_task_plan",
            "neo_get_messages", "neo_get_files", "neo_send_feedback",
            "neo_pause_task", "neo_resume_task", "neo_stop_task",
        ]
        for name in expected:
            self.assertIn(name, tool_names, f"Missing tool: {name}")

    async def test_tools_list_not_32602_error(self):
        """Regression: before the session-store fix, tools/list returned -32602."""
        async with self._make_client() as client:
            session_id = await self._do_handshake(client)
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._TOOLS_LIST_PAYLOAD),
                headers={**self._MCP_HEADERS, "mcp-session-id": session_id},
            )
        messages = self._parse_sse(resp.content)
        for msg in messages:
            if "error" in msg:
                self.assertNotEqual(
                    msg["error"].get("code"), -32602,
                    "Got -32602 (Invalid request parameters) — session state not persisted"
                )

    async def test_unknown_session_id_returns_404(self):
        """MCP protocol: an unrecognised mcp-session-id must return 404.

        The client must start fresh (no session ID header) to establish a new
        session.  Silently creating a new session would break the protocol.
        """
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._TOOLS_LIST_PAYLOAD),
                headers={**self._MCP_HEADERS, "mcp-session-id": "nonexistent-session-abc"},
            )
        self.assertEqual(resp.status_code, 404)

    async def test_session_id_is_unique_per_request(self):
        """Two independent initialize requests must receive different session IDs."""
        async with self._make_client() as client:
            r1 = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers=self._MCP_HEADERS,
            )
            r2 = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers=self._MCP_HEADERS,
            )
        self.assertNotEqual(
            r1.headers["mcp-session-id"],
            r2.headers["mcp-session-id"],
        )

    async def test_session_reuse_same_transport(self):
        """The same session ID reuses the existing transport (state persists)."""
        async with self._make_client() as client:
            session_id = await self._do_handshake(client)

            # tools/list on the same session must succeed
            r1 = await client.post(
                "/mcp",
                content=json.dumps(self._TOOLS_LIST_PAYLOAD),
                headers={**self._MCP_HEADERS, "mcp-session-id": session_id},
            )
            # A second tools/list on the same session must also succeed
            r2 = await client.post(
                "/mcp",
                content=json.dumps(self._TOOLS_LIST_PAYLOAD),
                headers={**self._MCP_HEADERS, "mcp-session-id": session_id},
            )

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Both responses must carry the same session ID
        self.assertEqual(r1.headers.get("mcp-session-id", session_id),
                         r2.headers.get("mcp-session-id", session_id))

    # --- Context var isolation ---

    async def test_per_session_key_stored_in_context(self):
        """The key supplied at initialize time is the key used for that session."""
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers={
                    "Authorization": "Bearer sk-v1-session-specific",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("mcp-session-id", resp.headers)

    # --- Health / routing ---

    async def test_health_endpoint(self):
        async with self._make_client() as client:
            resp = await client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["transport"], "http")

    async def test_root_endpoint(self):
        async with self._make_client() as client:
            resp = await client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    async def test_mcp_path_no_redirect(self):
        """/mcp must NOT redirect to /mcp/ — that would break session establishment."""
        async with self._make_client() as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers=self._MCP_HEADERS,
                follow_redirects=False,
            )
        self.assertNotIn(resp.status_code, (301, 302, 307, 308),
                         f"Got unexpected redirect {resp.status_code} → {resp.headers.get('location')}")

    async def test_initialized_notification_returns_202(self):
        async with self._make_client() as client:
            init_resp = await client.post(
                "/mcp",
                content=json.dumps(self._INIT_PAYLOAD),
                headers=self._MCP_HEADERS,
            )
            session_id = init_resp.headers["mcp-session-id"]
            notif_resp = await client.post(
                "/mcp",
                content=json.dumps(self._INITIALIZED_PAYLOAD),
                headers={**self._MCP_HEADERS, "mcp-session-id": session_id},
            )
        self.assertEqual(notif_resp.status_code, 202)


if __name__ == "__main__":
    unittest.main(verbosity=2)
