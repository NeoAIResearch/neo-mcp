"""Shared async HTTP probe used by every provider's test_connection."""

import time

import httpx

# Credential prefixes that provider APIs or misconfigured reverse proxies
# might echo back in an error response. If any of these appear in a response
# body we strip the body entirely — we'd rather give the user a generic
# "HTTP 4xx" than leak their key into the MCP tool response.
_CREDENTIAL_MARKERS = ("sk-ant-", "sk-or-", "hf_", "ghp_", "github_pat_")


def _sanitize_body(body: str, status_code: int) -> str:
    # Cap length first so we bound how much we scan.
    snippet = body[:200]
    for marker in _CREDENTIAL_MARKERS:
        if marker in snippet:
            return f"<redacted {status_code} body: contains credential-shaped token>"
    return snippet


async def probe(method: str, url: str, headers: dict, timeout: float = 10.0) -> tuple[bool, str, int]:
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — any failure = not reachable
        # Exception repr could theoretically surface a URL with embedded
        # credentials; none of our callers put secrets in URLs, but belt
        # and suspenders.
        msg = str(exc)
        for marker in _CREDENTIAL_MARKERS:
            if marker in msg:
                msg = "<redacted: exception message contained credential-shaped token>"
                break
        return False, f"request failed: {msg}", int((time.time() - start) * 1000)
    latency_ms = int((time.time() - start) * 1000)
    if resp.status_code == 200:
        return True, "ok", latency_ms
    return False, f"HTTP {resp.status_code}: {_sanitize_body(resp.text, resp.status_code)}", latency_ms


def parse_env_file(path) -> dict[str, str]:
    """Parse a simple KEY=VALUE per line env file. Ignores blanks + comments."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env
