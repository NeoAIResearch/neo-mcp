"""Kaggle credentials — stored via SecretStore + ~/.kaggle/kaggle.json."""

import json
import logging
import os
from pathlib import Path

from .._fsutil import atomic_write_secret
from ..secret_store import get_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "kaggle"
FIELDS = ("username", "key")

KAGGLE_JSON = Path.home() / ".kaggle" / "kaggle.json"


def write_secret(credentials: dict) -> dict:
    username = credentials["username"]
    key = credentials["key"]

    store = get_secret_store()
    store.write(PROVIDER, {"username": username, "key": key})

    KAGGLE_JSON.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_secret(KAGGLE_JSON, json.dumps({"username": username, "key": key}, indent=2) + "\n")

    return {
        "files_written": [store.location(PROVIDER), str(KAGGLE_JSON)],
        "backend": store.backend,
    }


def remove_secret() -> list[str]:
    store = get_secret_store()
    removed: list[str] = []
    if store.delete(PROVIDER, FIELDS):
        removed.append(store.location(PROVIDER))
    if KAGGLE_JSON.exists():
        KAGGLE_JSON.unlink()
        removed.append(str(KAGGLE_JSON))
    return removed


def load_env() -> dict[str, str]:
    creds = get_secret_store().read(PROVIDER, FIELDS)
    username = creds.get("username")
    key = creds.get("key")
    if not username or not key:
        return {}
    return {"KAGGLE_USERNAME": username, "KAGGLE_KEY": key}


async def test_connection() -> tuple[bool, str, int]:
    from ._http import probe
    env = load_env()
    username = env.get("KAGGLE_USERNAME")
    key = env.get("KAGGLE_KEY")
    if not username or not key:
        return False, "kaggle not configured", 0

    import base64
    token = base64.b64encode(f"{username}:{key}".encode()).decode()
    return await probe(
        "GET",
        "https://www.kaggle.com/api/v1/competitions/list?page=1&pageSize=1",
        {"Authorization": f"Basic {token}"},
    )
