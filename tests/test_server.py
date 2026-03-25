"""Unit tests for neo_mcp.server — no network calls, no Neo account required."""
import os
import sys
import tempfile
import unittest

# Provide a dummy key so the module can import without raising at startup
os.environ.setdefault("NEO_SECRET_KEY", "sk-v1-test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import neo_mcp.server as srv


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
        self.assertIn("deployment", msg.lower())


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
        # _save writes the value as-is; _load strips on read
        result = srv._load_thread_id()
        self.assertEqual(result, "thread-xyz")


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


class TestCheckConfig(unittest.TestCase):
    def test_raises_if_no_secret_key_in_stdio_mode(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_key = srv.NEO_SECRET_KEY
        try:
            srv.NEO_TRANSPORT = "stdio"
            srv.NEO_SECRET_KEY = ""
            with self.assertRaises(ValueError):
                srv._check_config()
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_SECRET_KEY = orig_key

    def test_no_raise_if_key_present_in_stdio_mode(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_key = srv.NEO_SECRET_KEY
        try:
            srv.NEO_TRANSPORT = "stdio"
            srv.NEO_SECRET_KEY = "sk-v1-test"
            srv._check_config()  # should not raise
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_SECRET_KEY = orig_key

    def test_no_raise_in_http_mode_without_key(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_key = srv.NEO_SECRET_KEY
        try:
            srv.NEO_TRANSPORT = "http"
            srv.NEO_SECRET_KEY = ""
            srv._check_config()  # HTTP mode: key is per-request, so no error
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_SECRET_KEY = orig_key


class TestHeaders(unittest.TestCase):
    def test_uses_env_secret_key(self):
        orig = srv.NEO_SECRET_KEY
        try:
            srv.NEO_SECRET_KEY = "sk-v1-mykey"
            headers = srv._headers()
            self.assertEqual(headers["Authorization"], "Bearer sk-v1-mykey")
        finally:
            srv.NEO_SECRET_KEY = orig

    def test_ctx_secret_key_takes_priority(self):
        orig = srv.NEO_SECRET_KEY
        try:
            srv.NEO_SECRET_KEY = "sk-v1-env-key"
            token = srv._ctx_secret_key.set("sk-v1-ctx-key")
            headers = srv._headers()
            self.assertEqual(headers["Authorization"], "Bearer sk-v1-ctx-key")
            srv._ctx_secret_key.reset(token)
        finally:
            srv.NEO_SECRET_KEY = orig


class TestResolveDeployment(unittest.TestCase):
    def test_stdio_always_vscode(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_dtype = srv.NEO_DEPLOYMENT_TYPE
        try:
            srv.NEO_TRANSPORT = "stdio"
            srv.NEO_DEPLOYMENT_TYPE = ""
            dtype, prefix = srv._resolve_deployment("some-dep-id")
            self.assertEqual(dtype, "vscode")
            self.assertIn("Working directory", prefix)
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_DEPLOYMENT_TYPE = orig_dtype

    def test_http_with_deployment_id_is_vscode(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_dtype = srv.NEO_DEPLOYMENT_TYPE
        try:
            srv.NEO_TRANSPORT = "http"
            srv.NEO_DEPLOYMENT_TYPE = ""
            dtype, prefix = srv._resolve_deployment("dep-123")
            self.assertEqual(dtype, "vscode")
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_DEPLOYMENT_TYPE = orig_dtype

    def test_http_without_deployment_id_is_cloud(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_dtype = srv.NEO_DEPLOYMENT_TYPE
        try:
            srv.NEO_TRANSPORT = "http"
            srv.NEO_DEPLOYMENT_TYPE = ""
            dtype, prefix = srv._resolve_deployment("")
            self.assertEqual(dtype, "cloud")
            self.assertEqual(prefix, "")
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_DEPLOYMENT_TYPE = orig_dtype

    def test_env_override_forces_cloud(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_dtype = srv.NEO_DEPLOYMENT_TYPE
        try:
            srv.NEO_TRANSPORT = "stdio"
            srv.NEO_DEPLOYMENT_TYPE = "cloud"
            dtype, prefix = srv._resolve_deployment("dep-123")
            self.assertEqual(dtype, "cloud")
            self.assertEqual(prefix, "")
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_DEPLOYMENT_TYPE = orig_dtype

    def test_env_override_forces_vscode(self):
        orig_transport = srv.NEO_TRANSPORT
        orig_dtype = srv.NEO_DEPLOYMENT_TYPE
        try:
            srv.NEO_TRANSPORT = "http"
            srv.NEO_DEPLOYMENT_TYPE = "vscode"
            dtype, prefix = srv._resolve_deployment("")
            self.assertEqual(dtype, "vscode")
        finally:
            srv.NEO_TRANSPORT = orig_transport
            srv.NEO_DEPLOYMENT_TYPE = orig_dtype


if __name__ == "__main__":
    unittest.main()
