"""AWS S3 credentials — stored via SecretStore + ~/.aws/credentials [neo] profile."""

import logging
import os
from pathlib import Path

from .._fsutil import atomic_write_secret
from ..secret_store import get_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "s3"
FIELDS = ("aws_access_key_id", "aws_secret_access_key", "region")

AWS_CREDENTIALS_FILE = Path.home() / ".aws" / "credentials"
_PROFILE = "neo"


def _read_aws_credentials() -> list[str]:
    if not AWS_CREDENTIALS_FILE.exists():
        return []
    return AWS_CREDENTIALS_FILE.read_text().splitlines()


def _write_neo_profile(key_id: str, secret: str, region: str) -> None:
    """Insert or replace the [neo] profile in ~/.aws/credentials."""
    lines = _read_aws_credentials()
    # Strip existing [neo] block
    out: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"[{_PROFILE}]":
            skip = True
            continue
        if skip and stripped.startswith("["):
            skip = False
        if not skip:
            out.append(line)

    # Append new block
    if out and out[-1].strip():
        out.append("")
    out += [
        f"[{_PROFILE}]",
        f"aws_access_key_id = {key_id}",
        f"aws_secret_access_key = {secret}",
    ]
    if region:
        out.append(f"region = {region}")
    out.append("")

    AWS_CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_secret(AWS_CREDENTIALS_FILE, "\n".join(out))


def _remove_neo_profile() -> bool:
    """Remove the [neo] profile from ~/.aws/credentials. Returns True if removed."""
    if not AWS_CREDENTIALS_FILE.exists():
        return False
    lines = _read_aws_credentials()
    out: list[str] = []
    skip = False
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"[{_PROFILE}]":
            skip = True
            removed = True
            continue
        if skip and stripped.startswith("["):
            skip = False
        if not skip:
            out.append(line)
    if not removed:
        return False
    # Trim trailing blank lines
    while out and not out[-1].strip():
        out.pop()
    if out:
        atomic_write_secret(AWS_CREDENTIALS_FILE, "\n".join(out) + "\n")
    else:
        AWS_CREDENTIALS_FILE.unlink(missing_ok=True)
    return True


def write_secret(credentials: dict) -> dict:
    key_id = credentials["aws_access_key_id"]
    secret = credentials["aws_secret_access_key"]
    region = credentials.get("region") or "us-east-1"

    store = get_secret_store()
    store.write(PROVIDER, {"aws_access_key_id": key_id, "aws_secret_access_key": secret, "region": region})
    _write_neo_profile(key_id, secret, region)

    return {
        "files_written": [store.location(PROVIDER), str(AWS_CREDENTIALS_FILE)],
        "backend": store.backend,
    }


def remove_secret() -> list[str]:
    store = get_secret_store()
    removed: list[str] = []
    if store.delete(PROVIDER, FIELDS):
        removed.append(store.location(PROVIDER))
    if _remove_neo_profile():
        removed.append(str(AWS_CREDENTIALS_FILE))
    return removed


def load_env() -> dict[str, str]:
    creds = get_secret_store().read(PROVIDER, FIELDS)
    key_id = creds.get("aws_access_key_id")
    secret = creds.get("aws_secret_access_key")
    if not key_id or not secret:
        return {}
    env: dict[str, str] = {
        "AWS_ACCESS_KEY_ID": key_id,
        "AWS_SECRET_ACCESS_KEY": secret,
        "AWS_PROFILE": _PROFILE,
    }
    region = creds.get("region")
    if region:
        env["AWS_DEFAULT_REGION"] = region
    return env


async def test_connection() -> tuple[bool, str, int]:
    env = load_env()
    if not env.get("AWS_ACCESS_KEY_ID"):
        return False, "s3 not configured", 0
    # Try boto3 for a proper STS check; fall back to connectivity-only probe.
    try:
        import boto3  # type: ignore
        import botocore.exceptions  # type: ignore
        import time

        start = time.time()
        sts = boto3.client(
            "sts",
            aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
            region_name=env.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        identity = sts.get_caller_identity()
        latency_ms = int((time.time() - start) * 1000)
        account = identity.get("Account", "unknown")
        return True, f"authenticated as account {account}", latency_ms
    except ImportError:
        pass
    except Exception as exc:
        import time
        return False, str(exc)[:120], 0

    # boto3 not available — just confirm S3 is reachable
    from ._http import probe
    ok, msg, latency = await probe("HEAD", "https://s3.amazonaws.com/", {})
    if ok or "403" in msg or "301" in msg:
        return True, "credentials stored (boto3 unavailable for full verification)", latency
    return False, msg, latency
