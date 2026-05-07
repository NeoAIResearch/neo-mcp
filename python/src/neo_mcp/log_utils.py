"""Daemon logger matching the VS Code extension's DaemonLogger format.

The extension writes runtime logs as:
    [<ISO timestamp>] [<LEVEL>] <message> <meta-json>\\n

with rotation when the log file is at least ``ROTATION_AGE_SECONDS`` old,
keeping ``MAX_ROTATED`` archive files (`.1` … `.4`). This module replicates
that contract for Python so ``~/.neo/daemon/neo-mcp.log`` is parseable by
the same readers as the extension's daemon log.

Birth-time portability: ``os.stat`` on Linux does not portably expose
file birth time (``st_birthtime`` is BSD/macOS only). Instead a sidecar
file ``<logfile>.birth`` records the Unix timestamp of first creation —
this matches the npm side and survives ext4/xfs/btrfs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROTATION_AGE_SECONDS: float = 12 * 60 * 60        # 12 h, matches Logger.ts:18
ROTATION_CHECK_INTERVAL_SECONDS: float = 60 * 60  # 1 h, matches Logger.ts:20
MAX_ROTATED: int = 4


def _format_timestamp(epoch_seconds: float, msec: int) -> str:
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{msec:03d}Z"


class RotatingDaemonHandler(logging.Handler):
    """``logging.Handler`` that produces extension-compatible log lines.

    Each emit:
      1. Checks file age via the ``.birth`` sidecar; rotates if ≥ 12 h.
      2. Appends ``[<ts>] [<LEVEL>] <msg> <meta>\\n`` to the log file.

    A periodic background thread also runs the rotation check hourly so
    long-running daemons that don't emit often still rotate on schedule.
    """

    def __init__(
        self,
        log_file: Path,
        deployment_id: str,
        rotation_age_seconds: float = ROTATION_AGE_SECONDS,
        max_rotated: int = MAX_ROTATED,
    ) -> None:
        super().__init__()
        self._log_file: Path = Path(log_file)
        self._deployment_id: str = deployment_id
        self._rotation_age: float = rotation_age_seconds
        self._max_rotated: int = max_rotated
        self._birth_file: Path = self._sibling(".birth")
        self._lock = threading.RLock()

        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_birth_recorded()

        self._timer_stop = threading.Event()
        self._timer = threading.Thread(
            target=self._rotation_loop,
            name="neo-mcp-log-rotator",
            daemon=True,
        )
        self._timer.start()

    def _sibling(self, ext: str) -> Path:
        """Return ``<logfile><ext>`` (e.g. ``neo-mcp.log.1`` / ``neo-mcp.log.birth``)."""
        return self._log_file.parent / f"{self._log_file.name}{ext}"

    def _archive_path(self, n: int) -> Path:
        return self._sibling(f".{n}")

    def _ensure_birth_recorded(self) -> None:
        try:
            if not self._log_file.exists():
                self._birth_file.write_text(f"{time.time():.0f}")
                return
            if not self._birth_file.exists():
                # Pre-existing log without sidecar (upgrade case) — backfill
                # with mtime as best-effort birth time.
                self._birth_file.write_text(f"{self._log_file.stat().st_mtime:.0f}")
        except OSError:
            # Read-only / permission errors — proceed without rotation tracking.
            pass

    def _file_age_seconds(self) -> float:
        try:
            birth = float(self._birth_file.read_text().strip())
        except (OSError, ValueError):
            return 0.0
        return max(0.0, time.time() - birth)

    def _rotate_if_needed(self) -> None:
        if not self._log_file.exists():
            return
        if self._file_age_seconds() < self._rotation_age:
            return
        try:
            oldest = self._archive_path(self._max_rotated)
            if oldest.exists():
                oldest.unlink()
            for i in range(self._max_rotated - 1, 0, -1):
                src = self._archive_path(i)
                if src.exists():
                    src.rename(self._archive_path(i + 1))
            self._log_file.rename(self._archive_path(1))
            self._birth_file.write_text(f"{time.time():.0f}")
        except OSError:
            # Don't let a rotation failure crash the process — best effort.
            pass

    def _rotation_loop(self) -> None:
        while not self._timer_stop.wait(ROTATION_CHECK_INTERVAL_SECONDS):
            with self._lock:
                self._rotate_if_needed()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock:
                self._rotate_if_needed()
                ts = _format_timestamp(record.created, int(record.msecs))
                meta: dict[str, object] = {
                    "deploymentId": self._deployment_id,
                    "logger": record.name,
                }
                if record.exc_info:
                    meta["exc"] = self.format(record) if False else \
                        logging.Formatter().formatException(record.exc_info)
                # Map Python's "WARNING" to "WARN" so logs match the extension/npm format.
                level = "WARN" if record.levelname == "WARNING" else record.levelname
                msg = record.getMessage()
                line = f"[{ts}] [{level}] {msg} {json.dumps(meta)}\n"
                with open(self._log_file, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception:  # noqa: BLE001 — handler must never raise
            self.handleError(record)

    def close(self) -> None:
        self._timer_stop.set()
        super().close()


def setup_daemon_logging(
    deployment_id: str,
    log_file: Path | str,
    level: int = logging.INFO,
) -> Optional[RotatingDaemonHandler]:
    """Install ``RotatingDaemonHandler`` as the sole root-logger handler.

    Returns the handler so callers (or tests) can interrogate or close it.
    On read-only/permission failures returns ``None`` and falls back to a
    stderr handler so the CLI stays usable.
    """
    log_path = Path(log_file)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    try:
        handler = RotatingDaemonHandler(log_path, deployment_id)
    except OSError:
        # Fallback to stderr — stdio-mode MCP would corrupt its own protocol
        # if we logged to stdout, but stderr is safe.
        import sys
        fallback = logging.StreamHandler(stream=sys.stderr)
        fallback.setLevel(level)
        fallback.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(fallback)
        return None
    handler.setLevel(level)
    root.addHandler(handler)
    return handler
