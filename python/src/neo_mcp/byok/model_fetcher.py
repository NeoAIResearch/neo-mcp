"""Fetch a provider's available model ids, with a hardcoded fallback.

With a key, query the provider's own model API (authoritative for that
account); without one — or on any failure — return the curated
``FALLBACK_MODELS`` list. Never raises.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .types import FALLBACK_MODELS

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0

# Anything matching these prefixes is not an OpenAI chat-completion model.
_OPENAI_EXCLUDE_PREFIXES = (
    "text-embedding", "text-moderation", "text-search", "text-similarity",
    "whisper", "tts-", "dall-e", "omni-moderation",
    "babbage", "davinci", "curie", "ada",
    "audio-", "transcribe-", "translate-", "gpt-image", "gpt-realtime",
    "gpt-oss-",
)


async def fetch_models(provider: str, api_key: Optional[str] = None) -> list[str]:
    try:
        if provider == "anthropic":
            return await _fetch_anthropic(api_key)
        if provider == "openai":
            return await _fetch_openai(api_key)
        if provider == "openrouter":
            return await _fetch_openrouter(api_key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_models(%s) failed: %s", provider, exc)
    return FALLBACK_MODELS.get(provider, [])


async def _fetch_anthropic(api_key: Optional[str]) -> list[str]:
    if not api_key:
        return FALLBACK_MODELS["anthropic"]
    models: list[str] = []
    after_id: Optional[str] = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        while True:
            params = {"limit": "100"}
            if after_id:
                params["after_id"] = after_id
            resp = await http.get(
                "https://api.anthropic.com/v1/models",
                params=params,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            if not resp.is_success:
                break
            data = resp.json()
            models.extend(m["id"] for m in data.get("data", []) if "id" in m)
            if not data.get("has_more"):
                break
            after_id = data.get("last_id")
    return models or FALLBACK_MODELS["anthropic"]


async def _fetch_openai(api_key: Optional[str]) -> list[str]:
    if not api_key:
        return FALLBACK_MODELS["openai"]
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        resp = await http.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if not resp.is_success:
            return FALLBACK_MODELS["openai"]
        ids = [m["id"] for m in resp.json().get("data", []) if "id" in m]
    chat = sorted(
        (i for i in ids
         if not any(i.startswith(p) for p in _OPENAI_EXCLUDE_PREFIXES) and ":" not in i),
        reverse=True,
    )
    # Merge with fallback so curated aliases always appear.
    merged = list(dict.fromkeys([*chat, *FALLBACK_MODELS["openai"]]))
    return merged or FALLBACK_MODELS["openai"]


async def _fetch_openrouter(api_key: Optional[str]) -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        resp = await http.get("https://openrouter.ai/api/v1/models", headers=headers)
        if not resp.is_success:
            return FALLBACK_MODELS["openrouter"]
        ids = sorted(m["id"] for m in resp.json().get("data", []) if "id" in m)
    return ids or FALLBACK_MODELS["openrouter"]
