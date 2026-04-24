"""Background poll loop — Python port of BackendPoller.ts.

Continuously polls the Neo backend for commands, dispatches them to
ActionHandlers, and sends responses back.

Backoff strategy (identical to TS):
  - base interval: 2 s
  - on consecutive errors: interval *= 1.5 per error, capped at 60 s
  - on success: reset to base interval

Thread status gate: commands for threads in TERMINATED / STOPPED states
are rejected with an error response (mirrors shouldAcceptCommands()).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .action_handlers import ActionHandlers
from .backend_client import BackendClient
from .config import POLL_BACKOFF_FACTOR, POLL_BASE_INTERVAL, POLL_MAX_INTERVAL, POLL_WAIT_TIME, TASK_TIMEOUT_HOURS, TASK_TIMEOUT_CHECK_INTERVAL
from .paths import DAEMON_DIR, DAEMON_LOG, THREAD_WORKSPACES_FILE

logger = logging.getLogger(__name__)
_TRACE_ROUTING = os.environ.get("NEO_TRACE_ROUTING", "").strip().lower() in {"1", "true", "yes", "on"}

_ACCEPTED_STATUSES = frozenset({"RUNNING", "PAUSED"})


class BackendPoller:
    """Runs as an asyncio background task."""

    # Max commands processed in parallel within a single poll batch.
    # High enough to keep all concurrent threads busy; low enough to avoid
    # overwhelming the local filesystem or spawning too many subprocesses.
    _MAX_CONCURRENT_COMMANDS = 32

    def __init__(
        self,
        deployment_id: str,
        client: BackendClient,
        handlers: ActionHandlers,
        thread_workspaces: dict[str, str],
    ) -> None:
        self._deployment_id = deployment_id
        self._client = client
        self._handlers = handlers
        self._thread_workspaces = thread_workspaces  # shared with ActionHandlers
        self._thread_statuses: dict[str, str] = {}
        self._running = False
        self._consecutive_errors = 0
        self._current_interval = POLL_BASE_INTERVAL
        # Semaphore limits concurrent command handlers so a large batch of
        # commands (e.g. 10 write_code + 10 run_subprocess) can all execute
        # in parallel without unbounded goroutine/task explosion.
        self._cmd_semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_COMMANDS)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Async entry point — run until cancelled or stop() is called."""
        import time
        self._running = True
        self._consecutive_errors = 0
        self._current_interval = POLL_BASE_INTERVAL
        last_command_time: float = 0.0    # monotonic clock, 0 = never
        last_cleanup_time: float = 0.0    # monotonic clock, 0 = never
        last_timeout_check: float = 0.0   # monotonic clock, 0 = never
        _CLEANUP_INTERVAL = 3600.0  # run cleanup_old_jobs every hour

        self._write_daemon_log()

        logger.info(
            "BackendPoller started: deployment_id=%s interval=%.1fs",
            self._deployment_id,
            self._current_interval,
        )

        while self._running:
            got_commands = False
            try:
                # During active execution use wait_time=1 so the poll returns quickly
                # after the backend queues the next command (reduces worst-case per-file
                # latency from ~5s to ~1s). Idle: use full POLL_WAIT_TIME (less traffic).
                recently_active = (time.monotonic() - last_command_time) < 60
                wait_time = 1 if recently_active else POLL_WAIT_TIME
                got_commands = await self._poll(wait_time=wait_time)
                if got_commands:
                    last_command_time = time.monotonic()
                # Success — reset backoff
                if self._consecutive_errors > 0:
                    self._consecutive_errors = 0
                    self._current_interval = POLL_BASE_INTERVAL
                    logger.debug("Poll succeeded — interval reset to %.1fs", self._current_interval)
            except asyncio.CancelledError:
                break
            except RuntimeError as exc:
                msg = str(exc)
                if msg == "DEPLOYMENT_NOT_FOUND":
                    logger.error("Deployment not found — stopping poller")
                    break
                if msg == "UNAUTHORIZED":
                    logger.error("Unauthorized — stopping poller")
                    break
                self._handle_error(exc)
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

            # Periodic job cleanup — every hour
            now_mono = time.monotonic()
            if now_mono - last_cleanup_time >= _CLEANUP_INTERVAL:
                self._handlers._job_manager.cleanup_old_jobs()
                last_cleanup_time = now_mono

            # Periodic stale-task auto-pause — every 5 minutes
            if TASK_TIMEOUT_HOURS > 0 and now_mono - last_timeout_check >= TASK_TIMEOUT_CHECK_INTERVAL:
                await self._check_and_pause_stale_tasks()
                last_timeout_check = now_mono

            if self._running and not got_commands:
                if recently_active:
                    # Small yield so the event loop can process cancellation/signals.
                    await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(self._current_interval)

        self._running = False
        logger.info("BackendPoller stopped")

    def stop(self) -> None:
        self._running = False

    def set_thread_status(self, thread_id: str, status: str) -> None:
        self._thread_statuses[thread_id] = status

    def register_thread_workspace(self, thread_id: str, workspace: str) -> None:
        """Register workspace for a new thread immediately after task submission.

        Must be called right after init_chat returns thread_id — before any
        poll commands arrive — so write_code uses the correct local path.
        The shared dict is also read by ActionHandlers, so no extra wiring needed.
        """
        self._thread_workspaces[thread_id] = workspace
        self._save_thread_workspaces()
        logger.info("Registered workspace for thread %s: %s", thread_id, workspace)

    def forget_thread(self, thread_id: str) -> None:
        """Evict a thread's workspace mapping and cached status.

        Called from _stop_task so that permanently-stopped threads don't
        accumulate in thread-workspaces.json. Safe to call for unknown
        thread IDs (no-op).
        """
        removed_workspace = self._thread_workspaces.pop(thread_id, None)
        self._thread_statuses.pop(thread_id, None)
        if removed_workspace is not None:
            self._save_thread_workspaces()
            logger.info("Forgot thread %s (was %s)", thread_id, removed_workspace)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_error(self, exc: Exception) -> None:
        self._consecutive_errors += 1
        backoff = min(
            POLL_BASE_INTERVAL * (POLL_BACKOFF_FACTOR ** min(self._consecutive_errors, 6)),
            POLL_MAX_INTERVAL,
        )
        self._current_interval = backoff
        logger.error(
            "Poll error #%d: %s — next attempt in %.1fs",
            self._consecutive_errors,
            exc,
            backoff,
        )

    async def _poll(self, wait_time: int = POLL_WAIT_TIME) -> bool:
        """Poll for commands and process them. Returns True if any commands were received."""
        commands = await self._client.poll_deployment(self._deployment_id, wait_time=wait_time)
        if not commands:
            return False
        logger.info("Received %d command(s)", len(commands))
        # Dispatch all commands in this batch concurrently — each command runs
        # independently (different thread/file), so there's no need to serialize them.
        await asyncio.gather(*[self._process_command(c) for c in commands])
        return True

    async def _process_command(self, command: dict[str, Any]) -> None:
        request_id = command.get("request_id", "")
        action = command.get("action", "")
        thread_id = command.get("thread_id")
        deployment_id = command.get("deployment_id") or self._deployment_id
        response_queue = command.get("response_queue_name")

        logger.info(
            "Command: action=%s request_id=%s thread_id=%s deployment_id=%s",
            action, request_id, thread_id, deployment_id,
        )

        # Thread status gate (mirrors shouldAcceptCommands())
        if thread_id and not self._should_accept(thread_id):
            status = self._thread_statuses.get(thread_id)
            error_resp = {
                "request_id": request_id,
                "sandbox_id": deployment_id,
                "status": "error",
                "error": f"Thread is {status} — not accepting commands",
                "thread_id": thread_id,
                "response_queue_name": response_queue,
            }
            await self._safe_send(error_resp)
            return

        # If this thread has no registered workspace, reload from disk —
        # covers the case where a daemon restart lost in-memory state.
        # NOTE: never fall back to command["workspace"] — that is the backend's
        # container path (e.g. /app/project), not the local path. Using it would
        # pass _is_allowed_path checks and write files to /app/project directly.
        workspace_source = "memory"
        if thread_id and thread_id not in self._thread_workspaces:
            fresh = BackendPoller._load_thread_workspaces()
            if thread_id in fresh:
                self._thread_workspaces.update(fresh)
                workspace_source = "disk-reload"
            else:
                workspace_source = "default-fallback"
                logger.warning(
                    "No workspace registered for thread %s — using default: %s",
                    thread_id, self._handlers._default_workspace,
                )

        try:
            async with self._cmd_semaphore:
                response = await self._handlers.handle_command(command)
        except Exception as exc:  # noqa: BLE001
            logger.error("Handler failed: %s", exc, exc_info=True)
            response = {"request_id": request_id, "status": "error", "error": str(exc)}

        # Attach routing fields required by backend
        response["sandbox_id"] = deployment_id
        if thread_id:
            response["thread_id"] = thread_id
            if _TRACE_ROUTING:
                logger.info(
                    "Routing trace: thread=%s workspace=%s source=%s action=%s",
                    thread_id,
                    self._thread_workspaces.get(thread_id, self._handlers._default_workspace),
                    workspace_source,
                    action,
                )
        if response_queue:
            response["response_queue_name"] = response_queue

        await self._safe_send(response)

    async def _safe_send(self, response: dict[str, Any]) -> None:
        delay = 0.5
        for attempt in range(1, 4):
            try:
                await self._client.send_response(self._deployment_id, response)
                return
            except Exception as exc:  # noqa: BLE001
                if attempt < 3:
                    logger.warning(
                        "sendResponse attempt %d failed for %s: %s — retrying in %.1fs",
                        attempt, response.get("request_id"), exc, delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error(
                        "sendResponse failed after 3 attempts for %s: %s",
                        response.get("request_id"), exc,
                    )

    def _should_accept(self, thread_id: str) -> bool:
        status = self._thread_statuses.get(thread_id)
        if status is None:
            return True  # no status tracked yet — allow (backwards compat)
        return status in _ACCEPTED_STATUSES

    async def _check_and_pause_stale_tasks(self) -> None:
        """Auto-pause threads that have been RUNNING or WAITING_FOR_FEEDBACK too long."""
        if not THREAD_WORKSPACES_FILE.exists():
            return
        try:
            raw = json.loads(THREAD_WORKSPACES_FILE.read_text())
        except Exception:  # noqa: BLE001
            return

        cutoff = datetime.now(timezone.utc).timestamp() - TASK_TIMEOUT_HOURS * 3600
        stale: list[str] = []
        for tid, val in raw.items():
            if not isinstance(val, dict):
                continue
            ts = val.get("updated_at")
            if isinstance(ts, (int, float)):
                age_ts = float(ts)
            elif isinstance(ts, str):
                try:
                    age_ts = datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    continue
            else:
                continue
            if age_ts < cutoff:
                stale.append(tid)

        for tid in stale:
            try:
                status_data = await self._client.get_thread_status(tid)
                status = status_data.get("status", "")
            except Exception:  # noqa: BLE001
                continue
            if status in ("RUNNING", "WAITING_FOR_FEEDBACK"):
                try:
                    await self._client.control_thread(tid, "PAUSE")
                    self.set_thread_status(tid, "PAUSED")
                    logger.warning(
                        "Auto-paused stale task %s (was %s, age > %.0fh)",
                        tid, status, TASK_TIMEOUT_HOURS,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not auto-pause stale task %s: %s", tid, exc)

    def _write_daemon_log(self) -> None:
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"sandboxId": self._deployment_id, "source": "neo-mcp"})
        try:
            with open(DAEMON_LOG, "a") as fh:
                fh.write(entry + "\n")
        except OSError as exc:
            logger.warning("Could not write daemon log: %s", exc)

    def _save_thread_workspaces(self) -> None:
        _THREAD_WORKSPACE_TTL_DAYS: float = float(os.environ.get("NEO_THREAD_WORKSPACES_TTL_SECONDS", 7 * 24 * 60 * 60)) / 86400
        _THREAD_WORKSPACE_MAX: int = int(os.environ.get("NEO_THREAD_WORKSPACES_MAX", 500))
        try:
            now = datetime.now(timezone.utc)
            cutoff = now.timestamp() - _THREAD_WORKSPACE_TTL_DAYS * 86400

            # Load existing timestamps so we preserve original updated_at (don't bump on every save)
            existing_ts: dict[str, float] = {}
            if THREAD_WORKSPACES_FILE.exists():
                try:
                    raw_existing = json.loads(THREAD_WORKSPACES_FILE.read_text())
                    for tid, val in raw_existing.items():
                        if isinstance(val, dict):
                            ua = val.get("updated_at")
                            if isinstance(ua, (int, float)):
                                existing_ts[tid] = float(ua)
                            elif isinstance(ua, str):
                                try:
                                    existing_ts[tid] = datetime.fromisoformat(ua).timestamp()
                                except ValueError:
                                    pass
                except Exception:  # noqa: BLE001
                    pass

            entries: list[tuple[str, str, float]] = []
            for tid, ws in self._thread_workspaces.items():
                if not ws:
                    continue
                # Keep original timestamp if workspace unchanged; else use now
                ts = existing_ts.get(tid, now.timestamp())
                if existing_ts.get(tid) and self._thread_workspaces.get(tid) != ws:
                    ts = now.timestamp()
                entries.append((tid, ws, ts))

            # Apply TTL filter + max-entries cap (keep newest) — mirrors npm saveThreadWorkspaces
            entries = [(tid, ws, ts) for tid, ws, ts in entries if ts >= cutoff]
            if len(entries) > _THREAD_WORKSPACE_MAX:
                entries = sorted(entries, key=lambda e: e[2])[-_THREAD_WORKSPACE_MAX:]

            data = {
                tid: {"workspace": ws, "updated_at": int(ts)}
                for tid, ws, ts in entries
            }
            # Atomic write: write to a temp file then rename so a crash mid-write
            # never leaves a corrupted file (matches npm daemon's renameSync pattern).
            THREAD_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = THREAD_WORKSPACES_FILE.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(THREAD_WORKSPACES_FILE)
        except OSError as exc:
            logger.warning("Could not save thread workspaces: %s", exc)

    @staticmethod
    def _load_thread_workspaces() -> dict[str, str]:
        if not THREAD_WORKSPACES_FILE.exists():
            return {}
        try:
            raw = json.loads(THREAD_WORKSPACES_FILE.read_text())
            result: dict[str, str] = {}
            for tid, val in raw.items():
                if isinstance(val, str):
                    result[tid] = val
                elif isinstance(val, dict):
                    ws = val.get("workspace")
                    if ws:
                        result[tid] = ws
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load thread workspaces: %s", exc)
            return {}
