"""Backend abstraction for storing provider credentials at rest.

Two implementations:

- ``FileStore`` — one ``~/.neo/integrations/<provider>.env`` file per provider,
  mode ``0o600``. Works everywhere (including headless Linux servers and Docker).
  Default.

- ``KeyringStore`` — OS keyring via the ``keyring`` package (macOS Keychain,
  Windows Credential Manager, Linux Secret Service / GNOME Keyring / KWallet).
  Encrypted at rest. Requires ``pip install neo-mcp[keyring]`` and (on Linux)
  a Secret Service daemon. Opt in with ``NEO_INTEGRATIONS_BACKEND=keyring``.

Backend selection happens **once per provider call** via ``get_secret_store()``
so tests can toggle the env var between calls.
"""

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..paths import INTEGRATIONS_DIR
from ._fsutil import atomic_write_secret

logger = logging.getLogger(__name__)

_KEYRING_SERVICE_PREFIX = "neo-mcp"


class SecretStore(ABC):
    """Per-provider credential bag. Fields within a provider are a flat dict."""

    @abstractmethod
    def write(self, provider: str, credentials: dict[str, str]) -> None: ...

    @abstractmethod
    def read(self, provider: str, fields: tuple[str, ...]) -> dict[str, str]: ...

    @abstractmethod
    def delete(self, provider: str, fields: tuple[str, ...]) -> bool: ...

    @property
    @abstractmethod
    def backend(self) -> str: ...

    @abstractmethod
    def location(self, provider: str) -> str:
        """Human-readable string describing where the provider's secrets live."""


class FileStore(SecretStore):
    """Store one .env file per provider under ``~/.neo/integrations/`` at 0o600."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self._dir = directory or INTEGRATIONS_DIR

    @property
    def backend(self) -> str:
        return "file"

    def _path(self, provider: str) -> Path:
        return self._dir / f"{provider}.env"

    def location(self, provider: str) -> str:
        return str(self._path(provider))

    def write(self, provider: str, credentials: dict[str, str]) -> None:
        # atomic_write_secret lands at 0o600 with no readable window —
        # the file never exists at default umask perms.
        lines = [f"{k}={v}" for k, v in credentials.items()]
        atomic_write_secret(self._path(provider), "\n".join(lines) + "\n")

    def read(self, provider: str, fields: tuple[str, ...]) -> dict[str, str]:
        path = self._path(provider)
        if not path.exists():
            return {}
        out: dict[str, str] = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
        # Filter to just the fields the caller asked for, if any given.
        if fields:
            return {k: v for k, v in out.items() if k in fields}
        return out

    def delete(self, provider: str, fields: tuple[str, ...]) -> bool:
        path = self._path(provider)
        if path.exists():
            path.unlink()
            return True
        return False


class KeyringStore(SecretStore):
    """Use the OS keyring as the at-rest store.

    Each (provider, field) pair becomes a keyring entry under service
    ``neo-mcp:<provider>`` with username ``<field>``. Raises RuntimeError on
    construction if the ``keyring`` package is not installed or no backend
    is available on this platform.
    """

    def __init__(self) -> None:
        try:
            import keyring  # noqa: F401
            import keyring.errors
        except ImportError as exc:
            raise RuntimeError(
                "keyring backend requested but the 'keyring' package is not "
                "installed — install with: pip install neo-mcp[keyring]"
            ) from exc
        self._keyring = keyring
        self._errors = keyring.errors
        # Probe: ensure at least one real backend is wired up. On Linux without
        # a Secret Service provider, keyring silently falls back to a "fail"
        # backend — detect that and tell the user up-front.
        try:
            backend_cls = keyring.get_keyring().__class__.__name__
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"keyring backend probe failed: {exc}") from exc
        if backend_cls in ("Keyring", "fail.Keyring"):
            raise RuntimeError(
                "keyring has no functional backend on this system "
                "(install keyrings.alt or a platform Secret Service)"
            )
        self._backend_name = f"keyring:{backend_cls}"

    @property
    def backend(self) -> str:
        return self._backend_name

    def _service(self, provider: str) -> str:
        return f"{_KEYRING_SERVICE_PREFIX}:{provider}"

    def location(self, provider: str) -> str:
        return self._service(provider)

    def write(self, provider: str, credentials: dict[str, str]) -> None:
        """Write all fields or none — if any field errors, roll back the successes.

        Keyring backends (D-Bus, KWallet) can drop mid-transaction; without
        rollback we'd leave orphan entries that confuse subsequent reads.
        """
        service = self._service(provider)
        written: list[str] = []
        try:
            for key, value in credentials.items():
                self._keyring.set_password(service, key, value)
                written.append(key)
        except BaseException:
            for key in written:
                try:
                    self._keyring.delete_password(service, key)
                except self._errors.KeyringError:
                    logger.warning(
                        "keyring rollback: could not remove partial field %s/%s",
                        provider, key,
                    )
            raise

    def read(self, provider: str, fields: tuple[str, ...]) -> dict[str, str]:
        service = self._service(provider)
        out: dict[str, str] = {}
        for field in fields:
            try:
                value = self._keyring.get_password(service, field)
            except self._errors.KeyringError as exc:
                logger.warning("keyring read failed for %s/%s: %s", provider, field, exc)
                continue
            if value:
                out[field] = value
        return out

    def delete(self, provider: str, fields: tuple[str, ...]) -> bool:
        service = self._service(provider)
        removed = False
        for field in fields:
            try:
                self._keyring.delete_password(service, field)
                removed = True
            except self._errors.PasswordDeleteError:
                continue
            except self._errors.KeyringError as exc:
                logger.warning("keyring delete failed for %s/%s: %s", provider, field, exc)
        return removed


def get_secret_store() -> SecretStore:
    """Pick a SecretStore based on ``NEO_INTEGRATIONS_BACKEND``.

    Values: ``file`` (default), ``keyring``. Unknown values fall back to file
    with a warning. If keyring is requested but unavailable, raises RuntimeError
    rather than silently writing plaintext — that's a security surprise we
    don't want to spring on the user.
    """
    backend = (os.environ.get("NEO_INTEGRATIONS_BACKEND") or "file").strip().lower()
    if backend == "keyring":
        return KeyringStore()
    if backend not in ("file", ""):
        logger.warning("Unknown NEO_INTEGRATIONS_BACKEND=%r — using file", backend)
    return FileStore()
