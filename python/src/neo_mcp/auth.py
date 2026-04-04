"""Authentication helpers — API key access and deployment ID derivation.

get_or_create_deployment_id() reads the machine-specific UUID from
~/.neo/daemon/standalone_deployment_id, or creates one on first use.
This ensures each machine gets a unique UUID even when the same API key
is used on multiple machines simultaneously.
"""

import os
import uuid
from pathlib import Path
from typing import Optional


def get_or_create_deployment_id(secret_key: str) -> str:
    """Return (or create) a machine-specific deployment UUID.

    1. Reads ~/.neo/daemon/standalone_deployment_id if it exists.
    2. Otherwise generates a fresh random UUID, writes it, and returns it.

    The UUID is random (not derived from the key) so two machines using the
    same API key each get their own UUID — preventing the backend from
    splitting commands across machines.
    """
    daemon_dir = Path.home() / ".neo" / "daemon"
    standalone_file = daemon_dir / "standalone_deployment_id"

    if standalone_file.exists():
        uid = standalone_file.read_text().strip()
        if uid:
            return uid

    daemon_dir.mkdir(parents=True, exist_ok=True)
    uid = str(uuid.uuid4())
    try:
        standalone_file.write_text(uid)
    except OSError:
        pass  # read-only filesystem edge case — use the generated UUID in memory
    return uid


def get_secret_key() -> Optional[str]:
    """Return NEO_SECRET_KEY from environment, or None if not set."""
    return os.environ.get("NEO_SECRET_KEY")
