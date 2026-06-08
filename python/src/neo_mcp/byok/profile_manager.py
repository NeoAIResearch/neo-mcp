"""BYOK profile management.

A *profile* is a named (provider, model) pair with an associated API key. The
profile metadata (id, name, provider, model) is persisted to
``~/.neo/settings.json`` — the same cross-process metadata file used for the
backend-env selection — while the API key is stored in the SecretStore
(``~/.neo/integrations/byok-<id>.env`` at 0o600, or the OS keyring when
``NEO_INTEGRATIONS_BACKEND=keyring``). Keys never land in settings.json.

When a profile is active, its credentials are resolved into the three
``x-llm-*`` headers and attached to exactly the two backend calls that drive
Neo's orchestrator (init-chat-direct + feedback).

Resolution precedence (``resolve_active_headers``):
  1. Active profile  → its key (error if the key is missing — never silently
     fall back to Neo's default).
  2. ``NEO_BYOK_KEY`` (+ ``NEO_BYOK_PROVIDER`` / ``NEO_BYOK_MODEL``) env vars →
     a zero-config path for ``claude mcp add -e …`` / CI.
  3. Nothing → no headers (backend uses Neo's own credentials).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from ..integrations._fsutil import atomic_write_secret, file_lock
from ..integrations.secret_store import SecretStore, get_secret_store
from ..paths import SETTINGS_FILE
from .types import (
    PROVIDER_LABELS,
    PROVIDERS,
    is_supported_provider,
    normalize_model_id,
)

logger = logging.getLogger(__name__)

_PROFILES_KEY = "byok_profiles"
_ACTIVE_KEY = "active_byok_profile_id"
_SECRET_FIELD = "api_key"


def _secret_provider(profile_id: str) -> str:
    """SecretStore namespace for a profile's key (filename-safe — no colons)."""
    return f"byok-{profile_id}"


class ByokError(Exception):
    """Raised for invalid BYOK operations (bad provider, missing profile, ...)."""


class ByokProfileManager:
    def __init__(
        self,
        settings_file: Path = SETTINGS_FILE,
        store: Optional[SecretStore] = None,
    ) -> None:
        self._settings_file = settings_file
        # Resolve the store lazily per call (get_secret_store re-reads the env
        # var each time), but allow injection for tests.
        self._store_override = store

    def _store(self) -> SecretStore:
        return self._store_override or get_secret_store()

    # ------------------------------------------------------------------
    # settings.json read / modify-write (preserves unrelated keys, e.g. "env")
    # ------------------------------------------------------------------

    def _read_settings(self) -> dict:
        try:
            with open(self._settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse %s: %s", self._settings_file, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _read_settings_strict(self) -> dict:
        """Like _read_settings but raises on a malformed file.

        Used before a read-modify-write so we never silently clobber a
        settings.json we couldn't parse — that would wipe the user's ``env``
        and any other keys. A missing file is fine (returns {}).
        """
        try:
            with open(self._settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ByokError(
                f"Refusing to modify {self._settings_file}: it is not valid JSON "
                f"({exc}). Fix or remove the file, then retry."
            ) from exc
        if not isinstance(data, dict):
            raise ByokError(
                f"Refusing to modify {self._settings_file}: top-level value is not "
                f"a JSON object."
            )
        return data

    def _write_settings(self, data: dict) -> None:
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_secret(
            self._settings_file,
            json.dumps(data, indent=2) + "\n",
            mode=0o600,
        )

    def _mutate_settings(self, fn) -> None:
        """Read-modify-write settings.json under a cross-process lock."""
        lock = self._settings_file.with_suffix(self._settings_file.suffix + ".lock")
        with file_lock(lock):
            data = self._read_settings_strict()
            fn(data)
            self._write_settings(data)

    # ------------------------------------------------------------------
    # Profile metadata
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[dict]:
        raw = self._read_settings().get(_PROFILES_KEY, [])
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for p in raw:
            if not isinstance(p, dict) or "id" not in p:
                continue
            out.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name", ""),
                    "provider": p.get("provider", ""),
                    "model": normalize_model_id(p.get("provider", ""), p.get("model", "")),
                }
            )
        return out

    def get_profile(self, profile_id: str) -> Optional[dict]:
        for p in self.list_profiles():
            if p["id"] == profile_id:
                return p
        return None

    def get_active_profile_id(self) -> Optional[str]:
        val = self._read_settings().get(_ACTIVE_KEY)
        return val if isinstance(val, str) else None

    def get_active_profile(self) -> Optional[dict]:
        pid = self.get_active_profile_id()
        return self.get_profile(pid) if pid else None

    def provider_label(self, provider: str) -> str:
        return PROVIDER_LABELS.get(provider, provider)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def save_profile(
        self,
        name: str,
        provider: str,
        model: str,
        api_key: str,
        profile_id: Optional[str] = None,
        set_active: bool = False,
    ) -> dict:
        if not is_supported_provider(provider):
            raise ByokError(
                f"Unsupported provider {provider!r}. Supported: {', '.join(PROVIDERS)}."
            )
        if not (name or "").strip():
            raise ByokError("Profile name is required.")
        if not (model or "").strip():
            raise ByokError("Model is required.")
        if not (api_key or "").strip():
            raise ByokError("API key is required.")

        pid = profile_id or str(uuid.uuid4())
        saved = {
            "id": pid,
            "name": name.strip(),
            "provider": provider,
            "model": normalize_model_id(provider, model.strip()),
        }

        # Persist the key first so a profile metadata entry never references a
        # missing secret.
        self._store().write(_secret_provider(pid), {_SECRET_FIELD: api_key.strip()})

        def _apply(data: dict) -> None:
            profiles = data.get(_PROFILES_KEY)
            if not isinstance(profiles, list):
                profiles = []
            idx = next((i for i, p in enumerate(profiles)
                        if isinstance(p, dict) and p.get("id") == pid), -1)
            if idx >= 0:
                profiles[idx] = saved
            else:
                profiles.append(saved)
            data[_PROFILES_KEY] = profiles
            if set_active:
                data[_ACTIVE_KEY] = pid

        self._mutate_settings(_apply)
        return saved

    def set_active(self, profile_id: Optional[str]) -> None:
        if profile_id is not None and self.get_profile(profile_id) is None:
            raise ByokError(f"No BYOK profile with id {profile_id!r}.")

        def _apply(data: dict) -> None:
            if profile_id is None:
                data.pop(_ACTIVE_KEY, None)
            else:
                data[_ACTIVE_KEY] = profile_id

        self._mutate_settings(_apply)

    def delete_profile(self, profile_id: str) -> None:
        if not profile_id:
            raise ByokError("profile_id is required.")

        def _apply(data: dict) -> None:
            profiles = data.get(_PROFILES_KEY)
            if isinstance(profiles, list):
                data[_PROFILES_KEY] = [
                    p for p in profiles
                    if not (isinstance(p, dict) and p.get("id") == profile_id)
                ]
            if data.get(_ACTIVE_KEY) == profile_id:
                data.pop(_ACTIVE_KEY, None)

        self._mutate_settings(_apply)
        # Best-effort key removal — a stale secret with no metadata is harmless
        # but we clean it up anyway.
        try:
            self._store().delete(_secret_provider(profile_id), (_SECRET_FIELD,))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not delete BYOK key for %s: %s", profile_id, exc)

    # ------------------------------------------------------------------
    # Keys + header resolution
    # ------------------------------------------------------------------

    def get_api_key(self, profile_id: str) -> Optional[str]:
        creds = self._store().read(_secret_provider(profile_id), (_SECRET_FIELD,))
        key = creds.get(_SECRET_FIELD)
        return key or None

    def key_hint(self, profile_id: str) -> str:
        key = self.get_api_key(profile_id)
        if not key:
            return ""
        if len(key) <= 4:
            return "••••"
        return f"••••••••{key[-4:]}"

    def resolve_headers(self, profile: dict) -> Optional[dict[str, str]]:
        api_key = self.get_api_key(profile["id"])
        if not api_key:
            return None
        return {
            "x-llm-key": api_key,
            "x-llm-provider": profile["provider"],
            "x-llm-model": profile["model"],
        }

    def _env_headers(self) -> Optional[dict[str, str]]:
        key = (os.environ.get("NEO_BYOK_KEY") or "").strip()
        provider = (os.environ.get("NEO_BYOK_PROVIDER") or "").strip().lower()
        model = (os.environ.get("NEO_BYOK_MODEL") or "").strip()
        if not key:
            return None
        if not is_supported_provider(provider):
            logger.warning(
                "NEO_BYOK_KEY set but NEO_BYOK_PROVIDER=%r is not one of %s — ignoring env BYOK.",
                provider, ", ".join(PROVIDERS),
            )
            return None
        if not model:
            logger.warning("NEO_BYOK_KEY set but NEO_BYOK_MODEL is empty — ignoring env BYOK.")
            return None
        return {
            "x-llm-key": key,
            "x-llm-provider": provider,
            "x-llm-model": normalize_model_id(provider, model),
        }

    def resolve_active_headers(self) -> tuple[Optional[dict[str, str]], Optional[str]]:
        """Resolve BYOK headers for an outgoing init-chat / feedback request.

        Returns ``(headers, error)``:
          - ``(headers, None)`` — attach these headers.
          - ``(None, error)``   — an active profile is selected but has no key;
            caller should surface ``error`` instead of proceeding.
          - ``(None, None)``    — no BYOK configured; send with Neo defaults.
        """
        profile = self.get_active_profile()
        if profile is not None:
            headers = self.resolve_headers(profile)
            if headers is None:
                return None, (
                    f"The active BYOK profile {profile['name']!r} has no API key. "
                    f"Re-add it with neo_add_byok_profile, or clear it with "
                    f"neo_set_byok_profile (profile_id: null)."
                )
            return headers, None
        return self._env_headers(), None
