"""Transactional ownership for mutable task workspaces."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from .ipc_state import read_json_object, update_json_object
from .paths import WORKSPACE_LEASES_FILE


def _canonical(workspace: str) -> str:
    return str(Path(workspace).expanduser().resolve())


def _key(workspace: str) -> str:
    return hashlib.sha256(_canonical(workspace).encode("utf-8")).hexdigest()


def acquire_workspace(
    workspace: str,
    submission_id: str,
    deployment_id: str,
) -> tuple[bool, dict[str, Any]]:
    """Reserve a workspace before remote task creation."""
    canonical = _canonical(workspace)
    key = _key(canonical)
    outcome: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> None:
        existing = data.get(key)
        if isinstance(existing, dict) and existing.get("submission_id") != submission_id:
            outcome.update(existing)
            return
        lease = {
            "workspace": canonical,
            "submission_id": submission_id,
            "deployment_id": deployment_id,
            "thread_id": existing.get("thread_id") if isinstance(existing, dict) else None,
            "state": "SUBMITTING",
            "updated_at": time.time(),
        }
        data[key] = lease
        outcome.update(lease)

    update_json_object(WORKSPACE_LEASES_FILE, mutate)
    return outcome.get("submission_id") == submission_id, outcome


def bind_workspace(workspace: str, submission_id: str, thread_id: str) -> None:
    key = _key(workspace)

    def mutate(data: dict[str, Any]) -> None:
        lease = data.get(key)
        if not isinstance(lease, dict) or lease.get("submission_id") != submission_id:
            raise RuntimeError("Workspace reservation was lost before task binding")
        lease.update({
            "thread_id": thread_id,
            "state": "ACTIVE",
            "updated_at": time.time(),
        })

    update_json_object(WORKSPACE_LEASES_FILE, mutate)


def release_workspace(
    *,
    workspace: str | None = None,
    submission_id: str | None = None,
    thread_id: str | None = None,
) -> bool:
    """Release only a matching owner; never steal a lease by age."""
    removed = False

    def mutate(data: dict[str, Any]) -> None:
        nonlocal removed
        for key, lease in list(data.items()):
            if not isinstance(lease, dict):
                continue
            matches = (
                (workspace is None or lease.get("workspace") == _canonical(workspace))
                and (submission_id is None or lease.get("submission_id") == submission_id)
                and (thread_id is None or lease.get("thread_id") == thread_id)
            )
            if matches:
                data.pop(key, None)
                removed = True

    update_json_object(WORKSPACE_LEASES_FILE, mutate)
    return removed


def lease_for_workspace(workspace: str) -> dict[str, Any] | None:
    value = read_json_object(WORKSPACE_LEASES_FILE).get(_key(workspace))
    return value if isinstance(value, dict) else None
