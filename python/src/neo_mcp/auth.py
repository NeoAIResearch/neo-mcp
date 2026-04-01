"""Authentication helpers — API key access and deployment ID derivation.

derive_deployment_id() produces the exact same UUID as:
  - VS Code extension: auth.ts deriveDeploymentId()
  - npm daemon: auth.ts deriveDeploymentId()

Algorithm: SHA-256(secret_key)[:16 bytes] → RFC 4122 UUID (version=5 bits forced).
Same key always → same UUID across all runtimes.
"""

import hashlib
import os
import uuid
from typing import Optional


def derive_deployment_id(secret_key: str) -> str:
    """Derive a deterministic deployment UUID from the API key."""
    digest = hashlib.sha256(secret_key.encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=5))


def get_secret_key() -> Optional[str]:
    """Return NEO_SECRET_KEY from environment, or None if not set."""
    return os.environ.get("NEO_SECRET_KEY")
