"""
neo-mcp login — browser-based OAuth flow for standalone pip installations.

Opens https://heyneo.so/login?redirect=http://localhost:{port}/callback,
waits for the auth callback, then writes the token to
~/.neo/daemon/mcp_auth.json so the daemon and server can use it.

Usage:
    neo-mcp login
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

NEO_AUTH_URL = os.environ.get("NEO_AUTH_URL", "https://heyneo.so")
_DAEMON_DIR = os.path.expanduser("~/.neo/daemon")
_MCP_AUTH_FILE = os.path.join(_DAEMON_DIR, "mcp_auth.json")

# ---------------------------------------------------------------------------
# Callback HTTP server
# ---------------------------------------------------------------------------

_received: dict = {}  # shared between threads: {"access_token", "refresh_token", "username"}
_server_done = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            _received["access_token"] = params.get("access_token", [""])[0]
            _received["refresh_token"] = params.get("refresh_token", [""])[0]
            _received["username"] = params.get("username", [""])[0]

            body = b"""<!DOCTYPE html>
<html>
<head><title>Neo Login</title>
<style>body{font-family:sans-serif;text-align:center;margin-top:80px;background:#0f0f0f;color:#fff;}
h1{color:#22c55e;}p{color:#a1a1aa;}</style></head>
<body>
<h1>&#10003; Authenticated</h1>
<p>You can close this tab and return to your terminal.</p>
</body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            _server_done.set()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # suppress default access log
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return an available TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def _prompt_manual_token() -> tuple[str, str, str]:
    """Fallback: ask user to paste their access token manually."""
    print()
    print("Browser login timed out or was cancelled.")
    print()
    print("Manual authentication:")
    print(f"  1. Open {NEO_AUTH_URL}/login in your browser")
    print("  2. Log in to your Neo account")
    print("  3. Open browser DevTools → Application → Local Storage")
    print("     (or check the auth callback URL for ?access_token=...)")
    print()
    try:
        token = input("Paste your access_token here (or press Enter to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        return "", "", ""
    if not token or len(token) < 10:
        return "", "", ""
    try:
        username = input("Enter your Neo username (email): ").strip()
    except (EOFError, KeyboardInterrupt):
        username = ""
    return token, "", username


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_login() -> None:
    port = _free_port()
    redirect_url = f"http://localhost:{port}/callback"
    login_url = f"{NEO_AUTH_URL}/login?redirect={redirect_url}"

    # Start local callback server in background thread
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    print()
    print("Opening Neo login in your browser…")
    print(f"  {login_url}")
    print()
    print("If the browser doesn't open automatically, paste the URL above into your browser.")
    print()

    opened = webbrowser.open(login_url)
    if not opened:
        print(f"Could not open browser automatically. Please open this URL manually:\n  {login_url}\n")

    # Wait up to 120 s for the callback
    completed = _server_done.wait(timeout=120)
    server.shutdown()

    if completed and _received.get("access_token"):
        access_token = _received["access_token"]
        refresh_token = _received.get("refresh_token", "")
        username = _received.get("username", "")
        _write_mcp_auth(access_token, refresh_token, username)
        print(f"Logged in as: {username or '(unknown)'}")
        print(f"Token saved to: {_MCP_AUTH_FILE}")
        print()
        print("You can now start the Neo daemon:")
        print("  neo-mcp daemon")
        return

    # Fallback: manual paste
    access_token, refresh_token, username = _prompt_manual_token()
    if access_token:
        _write_mcp_auth(access_token, refresh_token, username)
        print()
        print("Token saved.")
        print("You can now start the Neo daemon:")
        print("  neo-mcp daemon")
    else:
        print("Login cancelled — no token saved.")
        sys.exit(1)
