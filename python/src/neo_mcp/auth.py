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
import tempfile
import time
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

    return _read_or_create_standalone_uuid()


def _read_or_create_standalone_uuid() -> str:
    """Return the per-machine deployment UUID, atomically creating the file.

    Concurrency-safe via temp-file + ``os.link``: the candidate UUID is
    written into a temp file with full content, then linked into place.
    ``os.link`` fails if the target exists, so the loser re-reads the
    winner's value — eliminating the TOCTOU window where a plain
    ``O_EXCL | O_CREAT`` create lets readers observe an empty file before
    the winner finishes writing.
    """
    standalone_file = STANDALONE_UUID_FILE

    if standalone_file.exists():
        uid = standalone_file.read_text().strip()
        if uid:
            return uid

    parent = standalone_file.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only filesystem — fall through to in-memory UUID below.
        return str(uuid.uuid4())

    candidate = str(uuid.uuid4())
    fd, tmp_path = tempfile.mkstemp(
        dir=parent,
        prefix=f".{standalone_file.name}-",
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(candidate)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, standalone_file)
            return candidate
        except FileExistsError:
            # Another process won the race. Re-read the canonical file —
            # retry briefly because the winner may not have flushed yet.
            for _ in range(10):
                try:
                    existing = standalone_file.read_text().strip()
                except OSError:
                    existing = ""
                if existing:
                    return existing
                time.sleep(0.005)
            return candidate
    except OSError:
        return candidate
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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
