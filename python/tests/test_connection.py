"""Connectivity test — verifies the Neo backend is reachable and the secret key is valid.

Run with:
    NEO_SECRET_KEY=sk-v1-... python3 tests/test_connection.py

Exit codes:
    0  — backend reachable and key accepted
    1  — network error or auth failure
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

NEO_API_URL = os.environ.get("NEO_API_URL", "https://master.heyneo.so")
NEO_SECRET_KEY = os.environ.get("NEO_SECRET_KEY", "")


class TestBackendConnectivity(unittest.TestCase):

    @unittest.skipUnless(NEO_SECRET_KEY, "NEO_SECRET_KEY not set — skipping live connectivity test")
    def test_status_endpoint_reachable(self):
        """GET /v2/thread/status/ping-test should return 404 (not 401/502/timeout)."""
        import httpx
        url = f"{NEO_API_URL}/v2/thread/status/connectivity-test"
        headers = {"Authorization": f"Bearer {NEO_SECRET_KEY}"}
        try:
            resp = httpx.get(url, headers=headers, timeout=10)
        except httpx.ConnectError as e:
            self.fail(f"Could not connect to {NEO_API_URL}: {e}")
        except httpx.TimeoutException:
            self.fail(f"Request to {NEO_API_URL} timed out")

        # 401 means key is wrong; 502/503 means backend is down
        self.assertNotEqual(resp.status_code, 401, "NEO_SECRET_KEY was rejected (401)")
        self.assertNotIn(resp.status_code, (502, 503, 504), f"Backend unavailable ({resp.status_code})")
        # 404 is the expected response for a non-existent thread — that means auth passed
        self.assertIn(
            resp.status_code, (200, 404),
            f"Unexpected status {resp.status_code}: {resp.text[:200]}",
        )

    @unittest.skipUnless(NEO_SECRET_KEY, "NEO_SECRET_KEY not set — skipping live connectivity test")
    def test_backend_base_url_reachable(self):
        """Basic TCP/HTTP reachability of the Neo backend."""
        import httpx
        try:
            resp = httpx.get(NEO_API_URL, timeout=10, follow_redirects=True)
            # Any HTTP response (even 404) means the host is up
            self.assertIsNotNone(resp.status_code)
        except httpx.ConnectError as e:
            self.fail(f"Cannot reach {NEO_API_URL}: {e}")
        except httpx.TimeoutException:
            self.fail(f"Timed out connecting to {NEO_API_URL}")

    def test_no_secret_key_message(self):
        """Verify _headers() raises a clear error when key is missing."""
        import neo_mcp.server as srv
        orig_key = srv.NEO_SECRET_KEY
        try:
            srv.NEO_SECRET_KEY = ""
            srv._ctx_secret_key.set("")
            with self.assertRaises(ValueError) as ctx:
                srv._headers()
            self.assertIn("NEO_SECRET_KEY", str(ctx.exception))
        finally:
            srv.NEO_SECRET_KEY = orig_key


if __name__ == "__main__":
    # When run directly, always show results even for skipped tests
    verbosity = 2 if "-v" in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
