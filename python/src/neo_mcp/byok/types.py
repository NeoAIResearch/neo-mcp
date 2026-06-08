"""BYOK ("bring your own key") provider constants and helpers.

Three providers are supported, matching what the Neo backend accepts on the
``x-llm-*`` headers: ``anthropic``, ``openai``, ``openrouter``.

The fallback model lists are shown when no API key is available (Anthropic /
OpenAI) or when a live model fetch fails. IDs here must be valid for the
provider's own API — do NOT cross-source from OpenRouter (it uses dots in
version numbers; the native APIs use hyphens).
"""

from __future__ import annotations

PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "openrouter")

PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
}

# Shown when no API key is provided (Anthropic/OpenAI) or all live fetches fail.
# Curated per-provider fallback model lists.
FALLBACK_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-6",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-5-20251101",
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "o3",
        "o3-mini",
        "o4-mini",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
    ],
    # OpenRouter uses "provider/model" format with dots for version numbers.
    "openrouter": [
        "anthropic/claude-opus-4.7",
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "openai/gpt-5.4",
        "openai/gpt-5.4-mini",
        "openai/gpt-5.4-nano",
        "openai/gpt-5.4-pro",
        "openai/gpt-5.3-chat",
        "openai/gpt-5.2",
    ],
}


def normalize_model_id(provider: str, model_id: str) -> str:
    """Hyphenate dotted version numbers — Anthropic only.

    Anthropic ids use hyphens (``claude-opus-4-7``), so a user-entered
    ``claude-opus-4.7`` is normalized. OpenAI and OpenRouter are left untouched:
    both have real ids that contain dots (``gpt-4.1``,
    ``anthropic/claude-opus-4.7``), so replacing them would corrupt valid ids.

    OpenAI is intentionally NOT hyphenated: its real ids contain dots
    (``gpt-4.1``), so replacing them would produce invalid model ids.
    """
    if provider == "anthropic":
        return model_id.replace(".", "-")
    return model_id


def is_supported_provider(provider: str) -> bool:
    return provider in PROVIDERS
