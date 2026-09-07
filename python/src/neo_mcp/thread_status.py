"""Shared thread lifecycle status file — IPC between MCP tools and the daemon.

The Python stdio server is a thin client: it does not run BackendPoller.
Pause / resume / stop / feedback must still reach the detached daemon, so
status is persisted to ~/.neo/daemon/thread-statuses.json (atomic write).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .ipc_state import read_json_object, replace_json_object, update_json_object
from .paths import THREAD_STATUSES_FILE

logger = logging.getLogger(__name__)

_STATUS_MAX = int(os.environ.get("NEO_THREAD_STATUSES_MAX", "500"))


def load_thread_statuses() -> dict[str, str]:
    """Return thread_id → status. Missing/corrupt file → {}."""
    raw = read_json_object(THREAD_STATUSES_FILE)
    out: dict[str, str] = {}
    for tid, val in raw.items():
        if isinstance(val, str) and val:
            out[str(tid)] = val
        elif isinstance(val, dict):
            status = val.get("status")
            if isinstance(status, str) and status:
                out[str(tid)] = status
    return out


def thread_statuses_mtime() -> Optional[tuple[int, int, int]]:
    """Return a change signature robust to same-tick atomic replacements."""
    try:
        stat = THREAD_STATUSES_FILE.stat()
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError:
        return None


def write_thread_status(thread_id: str, status: str) -> None:
    """Upsert one thread's status and persist atomically.

    Safe to call from the thin-client MCP process — the live daemon reloads
    on mtime change. Empty thread_id or status is a no-op.
    """
    if not thread_id or not status:
        return
    def mutate(raw: dict) -> None:
        data = _normalize_raw(raw)
        data[thread_id] = {
            "status": status,
            "updated_at": int(datetime.now(timezone.utc).timestamp()),
        }
        if len(data) > _STATUS_MAX:
            ranked = sorted(
                data.items(),
                key=lambda kv: int(kv[1].get("updated_at", 0)),
            )
            data = dict(ranked[-_STATUS_MAX:])
        raw.clear()
        raw.update(data)

    try:
        update_json_object(THREAD_STATUSES_FILE, mutate)
    except OSError as exc:
        logger.warning("Could not save thread statuses: %s", exc)


def forget_thread_status(thread_id: str) -> None:
    """Remove a thread from the status file. No-op if unknown."""
    if not thread_id:
        return
    def mutate(raw: dict) -> None:
        data = _normalize_raw(raw)
        data.pop(thread_id, None)
        raw.clear()
        raw.update(data)

    try:
        update_json_object(THREAD_STATUSES_FILE, mutate)
    except OSError as exc:
        logger.warning("Could not save thread statuses: %s", exc)


def any_running(statuses: dict[str, str]) -> bool:
    return any(s == "RUNNING" for s in statuses.values())


def _load_raw() -> dict[str, dict]:
    return _normalize_raw(read_json_object(THREAD_STATUSES_FILE))


def _normalize_raw(raw: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tid, val in raw.items():
        if isinstance(val, str) and val:
            out[str(tid)] = {"status": val, "updated_at": 0}
        elif isinstance(val, dict) and isinstance(val.get("status"), str):
            out[str(tid)] = {
                "status": val["status"],
                "updated_at": int(val["updated_at"]) if isinstance(val.get("updated_at"), (int, float)) else 0,
            }
    return out


def _atomic_write(data: dict[str, dict]) -> None:
    try:
        replace_json_object(THREAD_STATUSES_FILE, data)
    except OSError as exc:
        logger.warning("Could not save thread statuses: %s", exc)
