"""Validate a BYOK key/model against the provider before persisting it.

Send a minimal chat completion and report success/failure, so an invalid
key/model is rejected before the profile is saved. Anthropic uses ``/v1/messages``;
OpenAI and OpenRouter use the OpenAI-compatible ``/v1/chat/completions`` with a
``max_tokens`` → ``max_completion_tokens`` retry for newer reasoning models.

The full API key is never logged.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .types import normalize_model_id

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 22.0
# Enough headroom for reasoning / "minimum output" models; 1 token often
# returns "could not finish".
_PROBE_MAX_TOKENS_LEGACY = 64
_PROBE_MAX_COMPLETION_TOKENS = 128
_ANTHROPIC_MAX_OUT = 64

_INVALID_KEY_MSG = (
    "Invalid API key — make sure you selected the correct provider and "
    "entered the right key."
)


def _describe_key(api_key: str) -> str:
    t = api_key.strip()
    if not t:
        return "(empty)"
    if len(t) <= 12:
        return f"(length {len(t)})"
    return f'prefix="{t[:4]}…" suffix="…{t[-4:]}" length={len(t)}'


def _error_message(resp: httpx.Response) -> str:
    text = resp.text
    try:
        j = resp.json()
        if isinstance(j, dict):
            msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else None
            msg = msg or j.get("message")
            if isinstance(msg, str) and msg:
                return msg
    except Exception:  # noqa: BLE001
        pass
    trimmed = text.strip()
    return trimmed[:280] if trimmed else (resp.reason_phrase or f"HTTP {resp.status_code}")


async def _post_chat_completion(
    http: httpx.AsyncClient,
    url: str,
    api_key: str,
    extra_headers: dict[str, str],
    body: dict,
) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        **extra_headers,
    }
    return await http.post(url, headers=headers, json=body)


async def _test_openai_compatible(
    http: httpx.AsyncClient,
    url: str,
    api_key: str,
    model: str,
    extra_headers: dict[str, str],
) -> tuple[bool, str]:
    base_body = {"model": model, "messages": [{"role": "user", "content": "ok"}]}
    resp = await _post_chat_completion(
        http, url, api_key, extra_headers, {**base_body, "max_tokens": _PROBE_MAX_TOKENS_LEGACY}
    )
    if resp.is_success:
        return True, ""

    err = _error_message(resp)
    lower = err.lower()
    retry_for_new_param = (
        "max_completion_tokens" in lower
        or ("max_tokens" in lower and "not supported" in lower)
    )
    retry_for_output_cap = (
        "max_tokens or model output limit" in lower
        or ("output limit" in lower and "max_tokens" in lower)
        or ("could not finish" in lower and "max_tokens" in lower)
    )
    if retry_for_new_param or retry_for_output_cap:
        resp = await _post_chat_completion(
            http, url, api_key, extra_headers,
            {**base_body, "max_completion_tokens": _PROBE_MAX_COMPLETION_TOKENS},
        )
        if resp.is_success:
            return True, ""
        return False, _error_message(resp)

    if resp.status_code == 401:
        return False, f"{_INVALID_KEY_MSG} ({err})"
    return False, err


async def _test_anthropic(
    http: httpx.AsyncClient, model: str, api_key: str
) -> tuple[bool, str]:
    resp = await http.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": _ANTHROPIC_MAX_OUT,
            "messages": [{"role": "user", "content": "ok"}],
        },
    )
    if resp.is_success:
        return True, ""
    err = _error_message(resp)
    if resp.status_code == 401:
        return False, f"{_INVALID_KEY_MSG} ({err})"
    return False, err


async def test_byok_credentials(
    provider: str, model: str, api_key: str
) -> tuple[bool, str]:
    """Send a minimal completion to ``provider`` using ``api_key`` and ``model``.

    Returns ``(ok, message)``; ``message`` is empty on success and a
    human-readable reason on failure. Never raises — network/timeout errors are
    returned as ``(False, message)``.
    """
    key = (api_key or "").strip()
    if not key:
        return False, "API key is required."
    m = normalize_model_id(provider, model)
    logger.info(
        "[BYOK credential test] provider=%s model=%s key=%s",
        provider, m, _describe_key(key),
    )

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as http:
            if provider == "openai":
                return await _test_openai_compatible(
                    http, "https://api.openai.com/v1/chat/completions", key, m, {}
                )
            if provider == "openrouter":
                return await _test_openai_compatible(
                    http,
                    "https://openrouter.ai/api/v1/chat/completions",
                    key,
                    m,
                    {"HTTP-Referer": "https://heyneo.so", "X-Title": "Neo MCP"},
                )
            if provider == "anthropic":
                return await _test_anthropic(http, m, key)
            return False, f"Unknown provider {provider!r}."
    except httpx.TimeoutException:
        return False, "Request timed out. Check your network and API reachability."
    except httpx.RequestError as exc:
        return False, str(exc) or "Request failed."
