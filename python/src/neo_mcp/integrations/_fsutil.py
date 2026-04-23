"""Filesystem helpers for safely handling credential files.

- ``atomic_write_secret`` — tempfile + ``os.replace`` so the destination
  never exists with default umask permissions. No TOCTOU window between
  "file created" and "mode 0o600 applied".

- ``file_lock`` — cross-process exclusive lock via ``fcntl.flock``.
  Used to serialize read-modify-write on ``~/.neo/integrations.json``
  so the pip MCP server and the VS Code extension can write to the
  shared metadata file concurrently without losing entries.

Unix only (fcntl). This is fine: the pip server targets Linux/macOS.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def atomic_write_secret(path: Path, content: str, mode: int = 0o600) -> None:
    """Write ``content`` to ``path`` atomically, landing at ``mode``.

    Creates the tempfile with ``tempfile.mkstemp`` (which on POSIX is
    already mode 0o600), writes, optionally fixes mode, then ``os.replace``.
    The destination file never exists readable by anyone other than the
    owner — there is no write-then-chmod window.

    On any failure the tempfile is unlinked so we don't leave ``.env.tmp``
    garbage behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        if mode != 0o600:
            os.chmod(tmp, mode)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # fdopen took ownership; do not close fd again
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive cross-process lock for the duration of the block.

    The lock file itself is distinct from the resource it protects (we don't
    want deleting the resource to release the lock mid-operation). Callers
    typically pass ``resource_path.with_suffix(resource_path.suffix + '.lock')``.

    Blocks until the lock is acquired. Released automatically on exit, even
    on exception.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open the lock file for writing; create if missing. We keep the fd open
    # only for the duration of the lock so restarts can reclaim cleanly.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
