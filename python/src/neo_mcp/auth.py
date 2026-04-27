"""Authentication helpers — API key access and deployment ID selection.

Default behavior mirrors VS Code daemon safety:
  - one machine-persisted UUID per host user
  - avoids cross-machine collisions when the same API key is reused

Override modes:
  - NEO_DEPLOYMENT_ID: explicit deployment UUID
  - NEO_DEPLOYMENT_ID_MODE=key-derived: deterministic UUID from API key
"""

import hashlib
import os
import uuid
from pathlib import Path
from typing import Optional

from neo_mcp.paths import STANDALONE_UUID_FILE


def derive_deployment_id(secret_key: str) -> str:
    """Derive deterministic UUID from API key (compatibility mode)."""
    digest = hashlib.sha256(secret_key.encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=5))


def get_or_create_deployment_id(secret_key: str) -> str:
    """Return deployment UUID using explicit override or machine default.

    Precedence:
    1. NEO_DEPLOYMENT_ID explicit override
    2. NEO_DEPLOYMENT_ID_MODE=key-derived (deterministic from key)
    3. machine-persisted standalone UUID (default)

    The default machine UUID prevents backend command fan-out collisions when
    one key is active on multiple machines at once.
    """
    explicit = os.environ.get("NEO_DEPLOYMENT_ID", "").strip()
    if explicit:
        return explicit

    mode = os.environ.get("NEO_DEPLOYMENT_ID_MODE", "").strip().lower()
    if mode in {"key-derived", "key", "deterministic"} and secret_key:
        return derive_deployment_id(secret_key)

    standalone_file = STANDALONE_UUID_FILE

    if standalone_file.exists():
        uid = standalone_file.read_text().strip()
        if uid:
            return uid

    standalone_file.parent.mkdir(parents=True, exist_ok=True)
    uid = str(uuid.uuid4())
    try:
        standalone_file.write_text(uid)
    except OSError:
        pass  # read-only filesystem edge case — use the generated UUID in memory
    return uid


def get_secret_key() -> Optional[str]:
    """Return NEO_SECRET_KEY from environment, or None if not set.

    Strips surrounding whitespace — config files (and in particular Claude
    Code's MCP env block in ~/.claude.json) can carry a stray trailing space,
    which httpx then rejects as ``Illegal header value`` and every poll fails
    silently in the background. Tolerating whitespace here means the user
    doesn't have to debug an opaque connectivity outage.
    """
    raw = os.environ.get("NEO_SECRET_KEY")
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None
