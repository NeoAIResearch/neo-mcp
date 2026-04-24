"""IntegrationManager — add/list/remove credentials; aggregate env for subprocesses.

Metadata file (``~/.neo/integrations.json``) is a shared contract with the
VS Code extension: it lists which providers are configured but holds **no**
secret values. The secrets live in each provider's native credential file.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..paths import INTEGRATIONS_METADATA_FILE
from ._fsutil import atomic_write_secret, file_lock
from .providers import MODULES
from .registry import PROVIDERS, IntegrationSchema

logger = logging.getLogger(__name__)


class ValidationError(ValueError):
    """Raised when a provider name is unknown or credentials fail schema checks."""


class IntegrationManager:
    def __init__(self, metadata_file: Optional[Path] = None) -> None:
        self._metadata_file = metadata_file or INTEGRATIONS_METADATA_FILE

    # ---- metadata file ---------------------------------------------------

    def _load_metadata(self) -> dict:
        if not self._metadata_file.exists():
            return {"version": 1, "integrations": {}}
        try:
            data = json.loads(self._metadata_file.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("integrations metadata corrupt; starting fresh")
            return {"version": 1, "integrations": {}}
        if not isinstance(data, dict):
            return {"version": 1, "integrations": {}}
        data.setdefault("version", 1)
        data.setdefault("integrations", {})
        if not isinstance(data["integrations"], dict):
            data["integrations"] = {}
        return data

    def _save_metadata(self, data: dict) -> None:
        atomic_write_secret(self._metadata_file, json.dumps(data, indent=2) + "\n")

    def _lock_path(self) -> Path:
        return self._metadata_file.with_suffix(self._metadata_file.suffix + ".lock")

    # ---- validation ------------------------------------------------------

    @staticmethod
    def _validate(schema: IntegrationSchema, credentials: dict) -> None:
        for field_name in schema.required_fields:
            value = credentials.get(field_name)
            if value is None or value == "":
                raise ValidationError(f"Missing required field: {field_name}")
        for field_name, pattern in schema.validators.items():
            value = credentials.get(field_name)
            if value and not re.match(pattern, str(value)):
                raise ValidationError(f"Invalid format for {field_name}")

    # ---- public API ------------------------------------------------------

    def list(self) -> list[dict]:
        """Return only integrations this server can actually use.

        The metadata file ~/.neo/integrations.json is shared with the VS
        Code extension, which writes entries under random IDs like
        "integration-1768977015296-3prpe3wd9" with display-cased provider
        names ("OpenRouter") inside the value. Our credential loaders
        (env_for_subprocess) key off MODULES, which only knows the
        canonical lowercase names — so extension-written entries never
        reach Neo subprocesses on this machine anyway. Showing them in
        the list just confuses the user into thinking the provider is
        configured when it is not.

        Strategy: include an entry only if its key case-folds to a known
        provider. The displayed "provider" is always canonical-lowercase
        so the list matches what the other tools (add/remove/test) accept.
        """
        data = self._load_metadata()
        known = {p.lower(): p for p in PROVIDERS.keys()}
        items: list[dict] = []
        for name, entry in data.get("integrations", {}).items():
            if not isinstance(entry, dict):
                continue
            canonical = known.get(str(name).lower())
            if canonical is None:
                continue  # extension-written ID key we can't use
            items.append({
                "provider": canonical,
                "method": entry.get("method"),
                "added_at": entry.get("added_at"),
                "files": entry.get("files", []),
            })
        return sorted(items, key=lambda x: x["provider"])

    def add(self, provider: str, credentials: dict) -> dict:
        if provider not in PROVIDERS:
            raise ValidationError(f"Unknown provider: {provider}")
        schema = PROVIDERS[provider]
        self._validate(schema, credentials)

        module = MODULES[provider]
        # Lock around the full transaction so concurrent writers (pip server
        # + VS Code extension, or rapid repeat calls) can't race on the
        # shared ~/.neo/integrations.json RMW.
        with file_lock(self._lock_path()):
            result = module.write_secret(credentials)

            data = self._load_metadata()
            data["integrations"][provider] = {
                "method": schema.method,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "files": result.get("files_written", []),
            }
            self._save_metadata(data)
        logger.info("integration added: %s (%s)", provider, schema.method)
        return {"provider": provider, "files_written": result.get("files_written", [])}

    def remove(self, provider: str) -> dict:
        if provider not in MODULES:
            raise ValidationError(f"Unknown provider: {provider}")
        module = MODULES[provider]
        with file_lock(self._lock_path()):
            removed = module.remove_secret()

            data = self._load_metadata()
            data["integrations"].pop(provider, None)
            self._save_metadata(data)
        logger.info("integration removed: %s", provider)
        return {"provider": provider, "removed_files": removed}

    async def test(self, provider: str) -> dict:
        if provider not in MODULES:
            raise ValidationError(f"Unknown provider: {provider}")
        ok, message, latency_ms = await MODULES[provider].test_connection()
        return {
            "provider": provider,
            "ok": ok,
            "message": message,
            "latency_ms": latency_ms,
        }

    def env_for_subprocess(self) -> dict[str, str]:
        """Aggregate load_env() from every configured provider.

        Called for every run_subprocess so Neo tasks inherit the user's
        API keys without having to re-supply them.
        """
        env: dict[str, str] = {}
        data = self._load_metadata()
        for provider in data.get("integrations", {}):
            module = MODULES.get(provider)
            if not module:
                continue
            try:
                env.update(module.load_env())
            except Exception as exc:  # noqa: BLE001 — never break subprocess launch
                logger.warning("load_env failed for %s: %s", provider, exc)
        return env
