"""Weights & Biases API key — stored via SecretStore + ~/.netrc."""

import logging
from pathlib import Path

from .._fsutil import atomic_write_secret
from ..secret_store import get_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "wandb"
FIELDS = ("api_key",)

NETRC_FILE = Path.home() / ".netrc"
_MACHINE = "api.wandb.ai"


def _read_netrc() -> list[str]:
    if not NETRC_FILE.exists():
        return []
    return NETRC_FILE.read_text().splitlines()


def _write_wandb_entry(api_key: str) -> None:
    """Insert or replace the api.wandb.ai block in ~/.netrc."""
    lines = _read_netrc()
    out: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"machine {_MACHINE}":
            skip = True
            continue
        if skip and stripped.startswith("machine "):
            skip = False
        if not skip:
            out.append(line)

    if out and out[-1].strip():
        out.append("")
    out += [
        f"machine {_MACHINE}",
        "  login user",
        f"  password {api_key}",
    ]

    atomic_write_secret(NETRC_FILE, "\n".join(out) + "\n")


def _remove_wandb_entry() -> bool:
    if not NETRC_FILE.exists():
        return False
    lines = _read_netrc()
    out: list[str] = []
    skip = False
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"machine {_MACHINE}":
            skip = True
            removed = True
            continue
        if skip and stripped.startswith("machine "):
            skip = False
        if not skip:
            out.append(line)
    if not removed:
        return False
    while out and not out[-1].strip():
        out.pop()
    if out:
        atomic_write_secret(NETRC_FILE, "\n".join(out) + "\n")
    else:
        NETRC_FILE.unlink(missing_ok=True)
    return True


def write_secret(credentials: dict) -> dict:
    api_key = credentials["api_key"]
    store = get_secret_store()
    store.write(PROVIDER, {"api_key": api_key})
    _write_wandb_entry(api_key)
    return {
        "files_written": [store.location(PROVIDER), str(NETRC_FILE)],
        "backend": store.backend,
    }


def remove_secret() -> list[str]:
    store = get_secret_store()
    removed: list[str] = []
    if store.delete(PROVIDER, FIELDS):
        removed.append(store.location(PROVIDER))
    if _remove_wandb_entry():
        removed.append(str(NETRC_FILE))
    return removed


def load_env() -> dict[str, str]:
    creds = get_secret_store().read(PROVIDER, FIELDS)
    key = creds.get("api_key")
    return {"WANDB_API_KEY": key} if key else {}


async def test_connection() -> tuple[bool, str, int]:
    from ._http import probe
    env = load_env()
    key = env.get("WANDB_API_KEY")
    if not key:
        return False, "wandb not configured", 0
    return await probe(
        "GET",
        "https://api.wandb.ai/graphql",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
