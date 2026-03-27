"""
neo-mcp login — browser-based OAuth flow for standalone pip installations.

Flow (remote/headless — primary):
  1. Generate a unique state UUID.
  2. Open https://heyneo.so/login?redirect=https://mcpserver.heyneo.com/auth/callback?state={uuid}
  3. User logs in on their browser (any device) — Neo redirects to the MCP server callback.
  4. CLI polls https://mcpserver.heyneo.com/auth/poll/{state} every 2 s until token arrives.
  5. Token written to ~/.neo/daemon/mcp_auth.json.

Flow (local — fast path):
  If a localhost port can be bound and the browser is on the same machine,
  the callback comes directly to the local HTTP server (faster, no relay needed).
  This is tried first; relay is used if the local server doesn't receive the
  callback within 8 s (likely running on a remote machine).

Usage:
    neo-mcp login
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
import urllib.request
import urllib.error

NEO_AUTH_URL = os.environ.get("NEO_AUTH_URL", "https://heyneo.so")
NEO_RELAY_URL = os.environ.get("NEO_RELAY_URL", "https://mcpserver.heyneo.com")
_DAEMON_DIR = os.path.expanduser("~/.neo/daemon")
_MCP_AUTH_FILE = os.path.join(_DAEMON_DIR, "mcp_auth.json")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_mcp_auth(access_token: str, refresh_token: str, username: str) -> None:
    os.makedirs(_DAEMON_DIR, exist_ok=True)
    data = {"access_token": access_token, "refresh_token": refresh_token, "username": username}
    tmp = _MCP_AUTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _MCP_AUTH_FILE)
    try:
        os.chmod(_MCP_AUTH_FILE, 0o600)
    except OSError:
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Local callback server (fast path for local installs)
# ---------------------------------------------------------------------------

_local_received: dict = {}
_local_done = threading.Event()


class _LocalCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            _local_received["access_token"] = params.get("access_token", [""])[0]
            _local_received["refresh_token"] = params.get("refresh_token", [""])[0]
            _local_received["username"] = params.get("username", [""])[0]
            body = b"""<!DOCTYPE html>
<html><head><title>Neo Login</title>
<style>body{font-family:sans-serif;text-align:center;margin-top:80px;background:#0f0f0f;color:#fff;}
h1{color:#22c55e;}p{color:#a1a1aa;}</style></head>
<body><h1>&#10003; Authenticated</h1>
<p>You can close this tab and return to your terminal.</p>
</body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            _local_done.set()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------------------
# Relay flow — works on remote servers (primary path)
# ---------------------------------------------------------------------------

def _relay_login() -> tuple[str, str, str]:
    """Open login URL that redirects to the MCP server relay, poll for the token.

    Returns (access_token, refresh_token, username) or ("", "", "") on failure.
    """
    state = str(uuid.uuid4())
    callback_url = f"{NEO_RELAY_URL}/auth/callback?state={state}"
    login_url = f"{NEO_AUTH_URL}/login?redirect={callback_url}"
    poll_url = f"{NEO_RELAY_URL}/auth/poll/{state}"
    pending_url = f"{NEO_RELAY_URL}/auth/pending/{state}"

    # Register state on relay server before showing URL so first poll gets 202
    try:
        req = urllib.request.Request(pending_url, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Non-fatal — relay server may not be reachable yet

    print()
    print("Open this URL in your browser to log in to Neo:")
    print(f"\n  {login_url}\n")
    print("Waiting for login", end="", flush=True)

    webbrowser.open(login_url)

    # Poll the MCP server relay for up to 3 minutes
    for i in range(90):
        time.sleep(2)
        print(".", end="", flush=True)
        try:
            resp = urllib.request.urlopen(poll_url, timeout=5)
            data = json.loads(resp.read())
            access_token = data.get("access_token", "")
            if access_token and len(access_token) >= 10:
                print(" done.")
                return access_token, data.get("refresh_token", ""), data.get("username", "")
        except urllib.error.HTTPError as e:
            if e.code == 410:
                # State expired server-side
                print()
                print("Login session expired. Please try again.")
                return "", "", ""
            # 202 = still pending — keep polling
        except Exception:
            pass  # Network blip — keep trying

    print()
    print("Login timed out (3 minutes).")
    return "", "", ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_login() -> None:
    # Try local callback server first (fast path — works if browser is on same machine)
    port = _free_port()
    local_redirect = f"http://localhost:{port}/callback"
    local_login_url = f"{NEO_AUTH_URL}/login?redirect={local_redirect}"

    local_server = HTTPServer(("127.0.0.1", port), _LocalCallbackHandler)
    t = threading.Thread(target=local_server.serve_forever, daemon=True)
    t.start()

    # Attempt to open the local-redirect URL silently.
    # If this machine is remote, the browser will be on a different host and
    # the callback will never arrive — we'll fall through to the relay flow.
    webbrowser.open(local_login_url)

    # Wait 8 s for the local callback (fast path)
    got_local = _local_done.wait(timeout=8)
    local_server.shutdown()

    if got_local and _local_received.get("access_token"):
        token = _local_received["access_token"]
        refresh = _local_received.get("refresh_token", "")
        username = _local_received.get("username", "")
        _write_mcp_auth(token, refresh, username)
        print(f"Logged in as: {username or '(unknown)'}")
        print(f"Token saved to: {_MCP_AUTH_FILE}")
        return

    # Local callback didn't fire — use relay flow (remote server / headless)
    access_token, refresh_token, username = _relay_login()

    if access_token:
        _write_mcp_auth(access_token, refresh_token, username)
        print(f"Logged in as: {username or '(unknown)'}")
        print(f"Token saved to: {_MCP_AUTH_FILE}")
        print()
        print("You can now start the Neo daemon:")
        print("  neo-mcp daemon")
        return

    # Last resort: manual paste
    print()
    print("Could not complete login automatically.")
    print(f"  1. Open {NEO_AUTH_URL}/login in your browser and log in")
    print("  2. After login, check the callback URL for ?access_token=...")
    print()
    try:
        token = input("Paste your access_token here (or press Enter to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        token = ""
    if token and len(token) >= 10:
        try:
            username = input("Enter your Neo username (email): ").strip()
        except (EOFError, KeyboardInterrupt):
            username = ""
        _write_mcp_auth(token, "", username)
        print("Token saved.")
    else:
        print("Login cancelled — no token saved.")
        sys.exit(1)
