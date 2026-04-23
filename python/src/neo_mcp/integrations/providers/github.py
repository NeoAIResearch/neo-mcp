"""GitHub PAT.

Canonical store: SecretStore (keyring or 0o600 file). In addition we write
the native ``~/.git-credentials`` line so ``git clone``/``push`` work
without extra configuration. On removal the github.com line is stripped from
``~/.git-credentials`` but other entries (gitlab.com, bitbucket.org) are
preserved.
"""

import logging
import subprocess
from pathlib import Path

from .._fsutil import atomic_write_secret
from ..secret_store import get_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "github"
FIELDS = ("pat", "username")

CREDENTIALS_FILE: Path = Path.home() / ".git-credentials"


def _rewrite_git_credentials(pat: str, username: str) -> None:
    """Replace any github.com entry in ~/.git-credentials with the new PAT."""
    existing = CREDENTIALS_FILE.read_text().splitlines() if CREDENTIALS_FILE.exists() else []
    kept = [line for line in existing if line.strip() and "@github.com" not in line]
    kept.append(f"https://{username}:{pat}@github.com")
    atomic_write_secret(CREDENTIALS_FILE, "\n".join(kept) + "\n")


def _strip_github_from_credentials() -> bool:
    if not CREDENTIALS_FILE.exists():
        return False
    existing = CREDENTIALS_FILE.read_text().splitlines()
    kept = [line for line in existing if line.strip() and "@github.com" not in line]
    if len(kept) == len(existing):
        return False
    if kept:
        atomic_write_secret(CREDENTIALS_FILE, "\n".join(kept) + "\n")
    else:
        CREDENTIALS_FILE.unlink()
    return True


def _configure_credential_helper() -> None:
    """Tell git to read ~/.git-credentials. Best-effort; silent on failure."""
    try:
        subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"],
            check=False, capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.info("Could not configure git credential.helper: %s", exc)


def write_secret(credentials: dict) -> dict:
    pat = credentials["pat"]
    username = credentials.get("username") or "git"

    store = get_secret_store()
    store.write(PROVIDER, {"pat": pat, "username": username})

    _rewrite_git_credentials(pat, username)
    _configure_credential_helper()

    return {
        "files_written": [store.location(PROVIDER), str(CREDENTIALS_FILE)],
        "backend": store.backend,
    }


def remove_secret() -> list[str]:
    store = get_secret_store()
    removed: list[str] = []
    if store.delete(PROVIDER, FIELDS):
        removed.append(store.location(PROVIDER))
    if _strip_github_from_credentials():
        removed.append(str(CREDENTIALS_FILE))
    return removed


def load_env() -> dict[str, str]:
    creds = get_secret_store().read(PROVIDER, FIELDS)
    pat = creds.get("pat")
    if not pat:
        return {}
    return {"GITHUB_TOKEN": pat, "GH_TOKEN": pat}


async def test_connection() -> tuple[bool, str, int]:
    from ._http import probe
    env = load_env()
    pat = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not pat:
        return False, "github not configured", 0
    return await probe(
        "GET",
        "https://api.github.com/user",
        {"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"},
    )
