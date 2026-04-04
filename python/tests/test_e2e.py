"""End-to-end integration tests against the live Neo backend.

Tests the full stack: real HTTP calls → MCP tool handlers → Neo API.
Requires a valid NEO_SECRET_KEY and a running daemon (VS Code extension
or `neo-mcp daemon`) to execute tasks.

Run with:
    NEO_SECRET_KEY=sk-v1-... python3 -m pytest tests/test_e2e.py -v

Sections:
  1. Auth & connectivity — key valid, endpoints reachable
  2. MCP tool smoke tests — tools callable, return correct shapes
  3. Task submission & status polling — real submit → poll → complete
  4. Daemon & deployment ID — detection paths work correctly
  5. Error handling — bad keys, bad thread IDs, network errors
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx

NEO_SECRET_KEY = os.environ.get("NEO_SECRET_KEY", "")
NEO_API_URL = os.environ.get("NEO_API_URL", "https://master.heyneo.so")

_NEEDS_KEY = unittest.skipUnless(NEO_SECRET_KEY, "NEO_SECRET_KEY not set")

# This module still contains legacy HTTP/server-shape assertions.
# Keep live coverage opt-in:
#   NEO_RUN_LEGACY_E2E=1 NEO_SECRET_KEY=... pytest -q tests/test_e2e.py
pytestmark = pytest.mark.skipif(
    os.environ.get("NEO_RUN_LEGACY_E2E") != "1",
    reason="Legacy end-to-end suite is opt-in; maintained suites run by default.",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(path: str, key: str = NEO_SECRET_KEY, timeout: int = 10) -> httpx.Response:
    return httpx.get(
        f"{NEO_API_URL}{path}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
    )


def _post(path: str, body: dict, key: str = NEO_SECRET_KEY, timeout: int = 15) -> httpx.Response:
    return httpx.post(
        f"{NEO_API_URL}{path}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. Auth & connectivity
# ---------------------------------------------------------------------------

class TestAuthAndConnectivity(unittest.TestCase):

    @_NEEDS_KEY
    def test_backend_reachable(self):
        resp = httpx.get(NEO_API_URL, timeout=10, follow_redirects=True)
        self.assertIsNotNone(resp.status_code)

    @_NEEDS_KEY
    def test_valid_key_accepted_on_status_endpoint(self):
        """404 on unknown thread = key accepted; 401 = key rejected."""
        resp = _get("/v2/thread/status/00000000-0000-0000-0000-000000000000")
        self.assertNotEqual(resp.status_code, 401, f"Key rejected: {resp.text[:200]}")
        self.assertNotIn(resp.status_code, (502, 503, 504), f"Backend down: {resp.status_code}")
        self.assertIn(resp.status_code, (200, 404))

    @_NEEDS_KEY
    def test_invalid_key_rejected(self):
        resp = _get("/v2/thread/status/00000000-0000-0000-0000-000000000000", key="sk-v1-invalid")
        self.assertEqual(resp.status_code, 401, f"Expected 401, got {resp.status_code}: {resp.text[:200]}")

    @_NEEDS_KEY
    def test_thread_messages_endpoint_reachable(self):
        resp = _get("/v2/thread/thread-messages?thread_id=00000000-0000-0000-0000-000000000000")
        self.assertNotEqual(resp.status_code, 401)
        self.assertNotIn(resp.status_code, (502, 503, 504))

    @_NEEDS_KEY
    def test_response_time_under_5s(self):
        start = time.time()
        _get("/v2/thread/status/00000000-0000-0000-0000-000000000000")
        elapsed = time.time() - start
        self.assertLess(elapsed, 5.0, f"Status endpoint took {elapsed:.1f}s (too slow)")


# ---------------------------------------------------------------------------
# 2. MCP tool smoke tests (via call_tool helper)
# ---------------------------------------------------------------------------

import mcp.types as mcp_types

def _set_key():
    import neo_mcp.server as srv
    # Keep existing in-process test key when no real key is provided.
    # This avoids mutating global module state in ways that break later tests.
    if NEO_SECRET_KEY:
        srv.NEO_SECRET_KEY = NEO_SECRET_KEY

def call_tool(name: str, arguments: dict | None = None):
    import neo_mcp.server as srv
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    handler = srv.app.request_handlers[mcp_types.CallToolRequest]
    result = asyncio.get_event_loop().run_until_complete(handler(req))
    return result.root.content

def text_of(result) -> str:
    return "\n".join(c.text for c in result if hasattr(c, "text"))


class TestMcpToolSmoke(unittest.TestCase):

    def setUp(self):
        _set_key()

    @_NEEDS_KEY
    def test_task_status_unknown_thread_returns_not_found(self):
        result = call_tool("neo_task_status", {"thread_id": "00000000-0000-0000-0000-000000000000"})
        txt = text_of(result)
        # Should get a not-found or error message, not a crash
        self.assertTrue(len(txt) > 0)
        self.assertNotIn("Traceback", txt)

    @_NEEDS_KEY
    def test_get_messages_unknown_thread_returns_gracefully(self):
        result = call_tool("neo_get_messages", {"thread_id": "00000000-0000-0000-0000-000000000000"})
        txt = text_of(result)
        self.assertTrue(len(txt) > 0)
        self.assertNotIn("Traceback", txt)

    @_NEEDS_KEY
    def test_missing_key_returns_clear_error(self):
        import neo_mcp.server as srv
        orig = srv.NEO_SECRET_KEY
        try:
            srv.NEO_SECRET_KEY = ""
            srv._ctx_secret_key.set("")
            result = call_tool("neo_task_status", {"thread_id": "test"})
            txt = text_of(result)
            self.assertIn("NEO_SECRET_KEY", txt)
        finally:
            srv.NEO_SECRET_KEY = orig

    @_NEEDS_KEY
    def test_all_9_tools_registered(self):
        import neo_mcp.server as srv
        req = mcp_types.ListToolsRequest()
        handler = srv.app.request_handlers[mcp_types.ListToolsRequest]
        result = _run(handler(req))
        tool_names = {t.name for t in result.root.tools}
        expected = {
            "neo_submit_task", "neo_task_status", "neo_get_messages",
            "neo_send_feedback", "neo_pause_task", "neo_resume_task",
            "neo_stop_task", "neo_task_plan", "neo_get_files",
        }
        self.assertEqual(tool_names, expected)

    @_NEEDS_KEY
    def test_read_only_mode_strips_write_tools(self):
        import neo_mcp.server as srv
        orig = srv.NEO_READ_ONLY
        try:
            srv.NEO_READ_ONLY = True
            req = mcp_types.ListToolsRequest()
            handler = srv.app.request_handlers[mcp_types.ListToolsRequest]
            result = _run(handler(req))
            tool_names = {t.name for t in result.root.tools}
            self.assertNotIn("neo_submit_task", tool_names)
            self.assertIn("neo_task_status", tool_names)
        finally:
            srv.NEO_READ_ONLY = orig


# ---------------------------------------------------------------------------
# 3. Task submission flow (requires daemon running)
# ---------------------------------------------------------------------------

class TestTaskSubmissionFlow(unittest.TestCase):
    """Full submit → poll → complete cycle.

    These tests require a daemon to be running locally (VS Code extension or
    `neo-mcp daemon`). They are skipped if no deployment_id is discoverable
    OR if submission returns 400 (no healthy deployment).
    """

    def setUp(self):
        _set_key()
        import neo_mcp.server as srv
        self._srv = srv
        self._srv._active_polls.clear()

    @_NEEDS_KEY
    def test_submit_returns_thread_id(self):
        """Real submission should return a valid thread_id UUID."""
        import neo_mcp.server as srv
        from unittest.mock import patch
        with patch("asyncio.create_task"):
            result = call_tool("neo_submit_task", {
                "description": "Print 'hello from neo-mcp e2e test' and exit.",
                "auto_mode": True,
            })
        txt = text_of(result)
        self.assertNotIn("Traceback", txt)
        # Either got a thread_id or a clear error (400 = no daemon, which is acceptable)
        if "400" in txt or "No healthy" in txt:
            self.skipTest("No daemon running — skipping submission test")
        # Should contain a thread_id
        import re
        self.assertTrue(
            re.search(r"[0-9a-f\-]{36}", txt),
            f"Expected thread_id UUID in response, got: {txt[:300]}"
        )

    @_NEEDS_KEY
    def test_submit_then_status_poll(self):
        """Submit a minimal task and poll status until terminal or timeout."""
        # First submit
        from unittest.mock import patch
        with patch("asyncio.create_task"):
            result = call_tool("neo_submit_task", {
                "description": "Echo 'e2e-status-check' to stdout.",
                "auto_mode": True,
            })
        txt = text_of(result)

        if "400" in txt or "No healthy" in txt:
            self.skipTest("No daemon running — skipping")

        # Extract thread_id
        import re
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", txt)
        if not m:
            self.skipTest(f"Could not extract thread_id from: {txt[:200]}")
        thread_id = m.group(1)

        # Poll status up to 60s
        deadline = time.time() + 60
        final_status = None
        while time.time() < deadline:
            resp = _get(f"/v2/thread/status/{thread_id}")
            if resp.status_code == 200:
                status = resp.json().get("status", "")
                if status in ("COMPLETED", "TERMINATED", "WAITING_FOR_FEEDBACK"):
                    final_status = status
                    break
            time.sleep(3)

        if final_status is None:
            self.skipTest("Task did not complete within 60s (daemon may be slow)")

        self.assertIn(final_status, ("COMPLETED", "TERMINATED", "WAITING_FOR_FEEDBACK"),
                      f"Unexpected final status: {final_status}")

    @_NEEDS_KEY
    def test_submit_with_wait_flag(self):
        """Submit with wait_for_completion=True; should return output inline."""
        result = call_tool("neo_submit_task", {
            "description": "Print the string 'e2e-wait-test-complete'.",
            "auto_mode": True,
            "wait_for_completion": True,
        })
        txt = text_of(result)

        if "400" in txt or "No healthy" in txt:
            self.skipTest("No daemon running")

        self.assertNotIn("Traceback", txt)
        # Should contain status info
        self.assertTrue(len(txt) > 10)


# ---------------------------------------------------------------------------
# 4. Daemon & deployment ID detection
# ---------------------------------------------------------------------------

class TestDaemonDetection(unittest.TestCase):

    def setUp(self):
        _set_key()

    def test_derive_deployment_id_is_deterministic(self):
        """Same key always produces the same UUID."""
        import neo_mcp.server as srv
        id1 = srv._derive_deployment_id("sk-v1-testkey")
        id2 = srv._derive_deployment_id("sk-v1-testkey")
        self.assertEqual(id1, id2)

    def test_derive_deployment_id_different_keys_differ(self):
        import neo_mcp.server as srv
        id1 = srv._derive_deployment_id("sk-v1-key-a")
        id2 = srv._derive_deployment_id("sk-v1-key-b")
        self.assertNotEqual(id1, id2)

    def test_derive_deployment_id_is_valid_uuid(self):
        import neo_mcp.server as srv
        import re
        dep_id = srv._derive_deployment_id("sk-v1-testkey")
        self.assertRegex(dep_id, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    @_NEEDS_KEY
    def test_real_key_derives_valid_uuid(self):
        import neo_mcp.server as srv
        import re
        dep_id = srv._derive_deployment_id(NEO_SECRET_KEY)
        self.assertRegex(dep_id, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        # Verify it matches what setup.py derives (must be identical)
        import neo_mcp.setup as setup_mod
        setup_id = setup_mod._derive_deployment_id(NEO_SECRET_KEY)
        self.assertEqual(dep_id, setup_id, "server.py and setup.py must derive the same ID from the same key")

    def test_vscode_extension_not_running_returns_false(self):
        """On a machine without VS Code extension on 31337, must return False quickly."""
        import socket
        import neo_mcp.setup as setup_mod
        import tempfile, os
        from unittest.mock import patch
        tmpdir = tempfile.mkdtemp()
        # No daemon.token → fast path False
        with patch.object(setup_mod, "_DAEMON_DIR", os.path.join(tmpdir, ".neo", "daemon")):
            result = setup_mod._vscode_extension_running()
        self.assertFalse(result)

    @_NEEDS_KEY
    def test_get_or_create_persistent_deployment_id_stable(self):
        """UUID persisted to file is stable across calls."""
        import neo_mcp.server as srv
        import tempfile
        from unittest.mock import patch
        tmpdir = tempfile.mkdtemp()
        fake_path = os.path.join(tmpdir, ".neo", "daemon", "standalone_deployment_id")
        with patch("neo_mcp.server.os.path.expanduser", side_effect=lambda p: p.replace("~", tmpdir)):
            id1 = srv._get_or_create_persistent_deployment_id(NEO_SECRET_KEY)
            id2 = srv._get_or_create_persistent_deployment_id(NEO_SECRET_KEY)
        self.assertEqual(id1, id2)

    def test_register_with_daemon_fails_fast_no_token(self):
        """_register_with_daemon returns False immediately when daemon.token absent."""
        import neo_mcp.server as srv
        import tempfile
        from unittest.mock import patch
        tmpdir = tempfile.mkdtemp()
        start = time.time()
        with patch("neo_mcp.server.os.path.expanduser", side_effect=lambda p: p.replace("~", tmpdir)):
            result = _run(srv._register_with_daemon("test-id", "sk-v1-test"))
        elapsed = time.time() - start
        self.assertFalse(result)
        self.assertLess(elapsed, 1.0, f"Should fail fast but took {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 5. Error handling — bad inputs, network robustness
# ---------------------------------------------------------------------------

class TestErrorHandling(unittest.TestCase):

    def setUp(self):
        _set_key()

    @_NEEDS_KEY
    def test_stop_nonexistent_task_returns_graceful_error(self):
        result = call_tool("neo_stop_task", {"thread_id": "00000000-0000-0000-0000-000000000000"})
        txt = text_of(result)
        self.assertTrue(len(txt) > 0)
        self.assertNotIn("Traceback", txt)

    @_NEEDS_KEY
    def test_send_feedback_nonexistent_thread_returns_error(self):
        result = call_tool("neo_send_feedback", {
            "thread_id": "00000000-0000-0000-0000-000000000000",
            "feedback": "test feedback",
        })
        txt = text_of(result)
        self.assertTrue(len(txt) > 0)
        self.assertNotIn("Traceback", txt)

    @_NEEDS_KEY
    def test_pause_nonexistent_thread_returns_error(self):
        result = call_tool("neo_pause_task", {"thread_id": "00000000-0000-0000-0000-000000000000"})
        txt = text_of(result)
        self.assertNotIn("Traceback", txt)

    @_NEEDS_KEY
    def test_handle_error_codes_match_backend_responses(self):
        """401 from backend maps to 'Invalid API key' message."""
        import neo_mcp.server as srv
        self.assertIn("Invalid API key", srv.handle_error(401))
        self.assertIn("credit", srv.handle_error(402).lower())
        self.assertIn("quota", srv.handle_error(403).lower())

    @_NEEDS_KEY
    def test_wrong_key_in_tool_call_returns_auth_error(self):
        import neo_mcp.server as srv
        from unittest.mock import patch
        with patch("neo_mcp.server.NEO_SECRET_KEY", "sk-v1-badkey"):
            result = call_tool("neo_task_status", {"thread_id": "00000000-0000-0000-0000-000000000000"})
        txt = text_of(result)
        self.assertIn("Invalid API key", txt)

    def test_background_poll_handles_bad_status_code(self):
        """Background poller must not crash on non-200 from status endpoint."""
        import neo_mcp.server as srv
        from unittest.mock import AsyncMock, MagicMock, patch

        async def mock_get(url, **kwargs):
            r = MagicMock()
            r.status_code = 503
            r.json.return_value = {}
            return r

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=mock_get)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        srv._active_polls.clear()
        # Run one iteration — should handle gracefully, not raise
        with patch("neo_mcp.server.httpx.AsyncClient", return_value=ctx), \
             patch("asyncio.sleep", new_callable=AsyncMock, side_effect=StopAsyncIteration):
            try:
                _run(srv._poll_task_bg("tid-503"))
            except StopAsyncIteration:
                pass  # Expected — we stopped the loop artificially
        # No exception leaked = pass


# ---------------------------------------------------------------------------
# 6. Full MCP server HTTP mode smoke (build the app, check routes exist)
# ---------------------------------------------------------------------------

class TestHttpAppRoutes(unittest.TestCase):

    def setUp(self):
        _set_key()

    def test_http_app_builds_without_error(self):
        """build_http_app() must not raise."""
        import neo_mcp.server as srv
        app = srv._build_http_app()
        self.assertIsNotNone(app)

    def test_http_app_has_mcp_route(self):
        """The Starlette app must respond on /mcp (POST without session → 400 or 401, not 404)."""
        import httpx
        import neo_mcp.server as srv
        app = srv._build_http_app()
        async def _check():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/mcp", content=b"{}", headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {NEO_SECRET_KEY}",
                })
                # /mcp exists — any non-404 response confirms the route is mounted
                self.assertNotEqual(resp.status_code, 404)
        _run(_check())

    def test_http_app_has_auth_routes(self):
        """Login relay /auth/* routes must respond (not 404)."""
        import httpx
        import neo_mcp.server as srv
        app = srv._build_http_app()
        async def _check():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # /auth/callback without params → 400 or redirect, not 404
                resp = await client.get("/auth/callback")
                self.assertNotEqual(resp.status_code, 404, "/auth/callback not found")
        _run(_check())


if __name__ == "__main__":
    verbosity = 2 if "-v" in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
