"""Cross-process-safe JSON state transactions for the MCP thin client/daemon."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .integrations._fsutil import file_lock


def read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object; missing, malformed, or non-object input becomes empty."""
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def update_json_object(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Lock the complete read-modify-fsync-replace transaction."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        data = read_json_object(path)
        mutate(data)
        _durable_replace(path, data)
        return data


def replace_json_object(path: Path, data: dict[str, Any]) -> None:
    """Replace a JSON object while holding the same cross-process lock."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        _durable_replace(path, data)


def _durable_replace(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        payload = json.dumps(data, indent=2).encode("utf-8")
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # Persist the directory entry where the platform supports directory fsync.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
