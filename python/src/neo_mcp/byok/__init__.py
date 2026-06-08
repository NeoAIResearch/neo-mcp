"""BYOK ("bring your own key") — run Neo's orchestrator on the user's own LLM key.

Named profiles (provider + model + key) whose credentials are attached as
``x-llm-*`` headers to the init-chat-direct and feedback backend calls.
"""

from .credentials_tester import test_byok_credentials
from .model_fetcher import fetch_models
from .profile_manager import ByokError, ByokProfileManager
from .types import (
    FALLBACK_MODELS,
    PROVIDER_LABELS,
    PROVIDERS,
    is_supported_provider,
    normalize_model_id,
)

__all__ = [
    "ByokError",
    "ByokProfileManager",
    "FALLBACK_MODELS",
    "PROVIDERS",
    "PROVIDER_LABELS",
    "fetch_models",
    "is_supported_provider",
    "normalize_model_id",
    "test_byok_credentials",
]
