"""OAuth 2.0 Authorization Server for Neo MCP Web Connector.

Implements RFC 8414 (authorization server metadata) and RFC 9728 (protected
resource metadata) so that Claude.ai and ChatGPT can discover and authenticate
with the Neo MCP server via OAuth 2.0 PKCE flow.

The "access token" issued IS the user's NEO_SECRET_KEY — the OAuth layer is
purely a UI/auth-discovery wrapper. Neo's backend already validates Bearer tokens.
"""

import base64
import hashlib
import html
import os
import secrets
import time
from typing import Any

# ---------------------------------------------------------------------------
# In-memory auth code store
# { code: { "secret_key": str, "code_challenge": str, "expires": float } }
# ---------------------------------------------------------------------------
_pending_codes: dict[str, dict[str, Any]] = {}

# Public base URL — override via NEO_PUBLIC_URL env var for local dev/testing
_BASE_URL = os.environ.get("NEO_PUBLIC_URL", "https://mcp.heyneo.so")

_CODE_EXPIRY_SECONDS = 600  # 10 minutes


def _verify_pkce(verifier: str, challenge: str) -> bool:
    """Verify PKCE S256: sha256(verifier) == base64url(challenge)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == challenge


def _clean_expired_codes() -> None:
    now = time.time()
    expired = [k for k, v in _pending_codes.items() if v["expires"] < now]
    for k in expired:
        del _pending_codes[k]


def _is_safe_redirect_uri(uri: str) -> bool:
    """Allow only https:// or http://localhost redirect URIs (no open redirect)."""
    return uri.startswith("https://") or uri.startswith("http://localhost")


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------

async def oauth_protected_resource(_request: Any) -> Any:
    """RFC 9728 — Protected Resource Metadata."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "resource": f"{_BASE_URL}/mcp",
        "authorization_servers": [_BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["neo:tasks"],
        "resource_documentation": "https://github.com/NeoResearchAI/MCPServer",
    })


async def oauth_authorization_server(_request: Any) -> Any:
    """RFC 8414 — Authorization Server Metadata."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "issuer": _BASE_URL,
        "authorization_endpoint": f"{_BASE_URL}/oauth/authorize",
        "token_endpoint": f"{_BASE_URL}/oauth/token",
        "revocation_endpoint": f"{_BASE_URL}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["neo:tasks"],
    })


# ---------------------------------------------------------------------------
# Authorization endpoint — renders key-entry page, issues auth code
# ---------------------------------------------------------------------------

_AUTHORIZE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Connect Neo</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 2rem 2.5rem;
      max-width: 420px;
      width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,0.10);
    }}
    h2 {{ margin-top: 0; color: #111; font-size: 1.4rem; }}
    p {{ color: #555; font-size: 0.95rem; line-height: 1.5; }}
    label {{ font-size: 0.9rem; color: #333; }}
    input[type=password] {{
      width: 100%;
      padding: 0.65rem 0.75rem;
      font-size: 1rem;
      border: 1px solid #ccc;
      border-radius: 6px;
      box-sizing: border-box;
      margin: 0.4rem 0 1rem;
    }}
    button {{
      width: 100%;
      padding: 0.7rem;
      background: #0070f3;
      color: white;
      border: none;
      border-radius: 6px;
      font-size: 1rem;
      cursor: pointer;
    }}
    button:hover {{ background: #005bb5; }}
    .logo {{ font-weight: 700; font-size: 1.1rem; color: #0070f3; margin-bottom: 1rem; }}
    a {{ color: #0070f3; }}
    code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">Neo</div>
    <h2>Connect Neo to your AI assistant</h2>
    <p>Enter your Neo secret key (<code>sk-v1-...</code>) from the
       <a href="https://app.heyneo.so" target="_blank" rel="noopener">Neo dashboard</a>.</p>
    <form method="POST" action="/oauth/authorize">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state" value="{state}">
      <input type="hidden" name="code_challenge" value="{code_challenge}">
      <input type="hidden" name="client_id" value="{client_id}">
      <label for="secret_key">Secret key</label>
      <input type="password" id="secret_key" name="secret_key"
             placeholder="sk-v1-..." autocomplete="off" required>
      <button type="submit">Authorize</button>
    </form>
  </div>
</body>
</html>"""


async def oauth_authorize(request: Any) -> Any:
    from starlette.responses import HTMLResponse, RedirectResponse

    if request.method == "GET":
        response_type = request.query_params.get("response_type", "code")
        code_challenge_method = request.query_params.get("code_challenge_method", "S256")
        redirect_uri = request.query_params.get("redirect_uri", "")

        if response_type != "code":
            return HTMLResponse("<h1>Unsupported response_type — only 'code' is supported</h1>", status_code=400)
        if code_challenge_method != "S256":
            return HTMLResponse("<h1>Only S256 code_challenge_method is supported</h1>", status_code=400)
        if not redirect_uri or not _is_safe_redirect_uri(redirect_uri):
            return HTMLResponse("<h1>Invalid or missing redirect_uri</h1>", status_code=400)

        page = _AUTHORIZE_HTML.format(
            redirect_uri=html.escape(redirect_uri),
            state=html.escape(request.query_params.get("state", "")),
            code_challenge=html.escape(request.query_params.get("code_challenge", "")),
            client_id=html.escape(request.query_params.get("client_id", "")),
        )
        return HTMLResponse(page)

    # POST — process form submission
    form = await request.form()
    secret_key = (form.get("secret_key") or "").strip()
    redirect_uri = (form.get("redirect_uri") or "").strip()
    state = (form.get("state") or "").strip()
    code_challenge = (form.get("code_challenge") or "").strip()

    if not secret_key:
        return HTMLResponse("<h1>Secret key is required</h1>", status_code=400)
    if not redirect_uri or not _is_safe_redirect_uri(redirect_uri):
        return HTMLResponse("<h1>Invalid redirect_uri</h1>", status_code=400)

    _clean_expired_codes()
    code = secrets.token_urlsafe(32)
    _pending_codes[code] = {
        "secret_key": secret_key,
        "code_challenge": code_challenge,
        "expires": time.time() + _CODE_EXPIRY_SECONDS,
    }

    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint — PKCE exchange → returns secret key as access_token
# ---------------------------------------------------------------------------

async def oauth_token(request: Any) -> Any:
    from starlette.responses import JSONResponse

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body: dict = await request.json()
        except Exception:
            body = {}
    else:
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type", "")
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = body.get("code", "")
    code_verifier = body.get("code_verifier", "")

    _clean_expired_codes()
    entry = _pending_codes.get(code)
    if not entry:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Unknown or expired authorization code"},
            status_code=400,
        )

    if time.time() > entry["expires"]:
        del _pending_codes[code]
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Authorization code has expired"},
            status_code=400,
        )

    # Enforce PKCE if a challenge was supplied at authorize time
    if entry["code_challenge"]:
        if not code_verifier:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "code_verifier is required"},
                status_code=400,
            )
        if not _verify_pkce(code_verifier, entry["code_challenge"]):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )

    secret_key = entry["secret_key"]
    del _pending_codes[code]  # single-use

    return JSONResponse({
        "access_token": secret_key,
        "token_type": "bearer",
        "scope": "neo:tasks",
        "expires_in": 7776000,  # 90 days
    })


# ---------------------------------------------------------------------------
# Revocation endpoint — no-op (tokens are stateless NEO_SECRET_KEYs)
# ---------------------------------------------------------------------------

async def oauth_revoke(_request: Any) -> Any:
    from starlette.responses import JSONResponse
    return JSONResponse({}, status_code=200)


# ---------------------------------------------------------------------------
# Route list — imported by server._run_http()
# ---------------------------------------------------------------------------

def oauth_routes() -> list:
    """Return Starlette Route objects for all OAuth + discovery endpoints."""
    from starlette.routing import Route
    return [
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server, methods=["GET"]),
        Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Route("/oauth/revoke", oauth_revoke, methods=["POST"]),
    ]
