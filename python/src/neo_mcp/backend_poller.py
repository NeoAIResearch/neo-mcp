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
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .action_handlers import ActionHandlers
from .backend_client import BackendClient
from .config import (
    POLL_BACKOFF_FACTOR,
    POLL_BASE_INTERVAL,
    POLL_DRAIN_EMPTY,
    POLL_EMPTY_STREAK_BEFORE_STATUS,
    POLL_MAX_INTERVAL,
    POLL_PARK_TICK,
    POLL_WAIT_TIME,
)
from .command_state import (
    append_evidence,
    claim_command,
    complete_command,
    mark_delivered,
    pending_responses,
)
from .ipc_state import read_json_object, replace_json_object, update_json_object
from .paths import (
    DAEMON_DIR,
    DAEMON_LOG,
    THREAD_WORKSPACES_FILE,
    deployment_ready_file,
)
from .service import package_version
from .thread_status import (
    any_running,
    load_thread_statuses,
    thread_statuses_mtime,
    write_thread_status,
)
from .workspace_leases import release_workspace

logger = logging.getLogger(__name__)
_TRACE_ROUTING = os.environ.get("NEO_TRACE_ROUTING", "").strip().lower() in {"1", "true", "yes", "on"}

# RUNNING always accepted. PAUSED only during the short drain after the last
# RUNNING thread leaves, so in-flight wrap-up commands can finish.
_ALWAYS_ACCEPTED = frozenset({"RUNNING"})
_DRAIN_ACCEPTED = frozenset({"RUNNING", "PAUSED"})
_TERMINAL_STATUSES = frozenset({
    "STOPPING", "STOPPED", "TERMINATED", "FAILED", "COMPLETED", "CANCELLED",
})


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
        thread_wrappers: Optional[dict[str, list[str]]] = None,
    ) -> None:
        self._deployment_id = deployment_id
        self._client = client
        self._handlers = handlers
        self._thread_workspaces = thread_workspaces  # shared with ActionHandlers
        # Shared with ActionHandlers — pre-seed via register_thread_wrapper so
        # the very first relative-path command for a thread (e.g. `mkdir -p
        # <slug>/plans`) gets the wrapper stripped instead of creating a stray
        # <slug>/ folder at workspace root. If caller doesn't supply one, fall
        # back to handlers' own dict so existing wiring keeps working.
        self._thread_wrappers: dict[str, list[str]] = (
            thread_wrappers if thread_wrappers is not None else handlers._thread_wrappers
        )
        self._thread_statuses: dict[str, str] = load_thread_statuses()
        self._status_mtime = thread_statuses_mtime()
        self._draining = False
        self._drain_remaining = 0
        self._park_tick = POLL_PARK_TICK
        self._drain_empty = POLL_DRAIN_EMPTY
        self._empty_streak_before_status = POLL_EMPTY_STREAK_BEFORE_STATUS
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
        empty_streak = 0
        _CLEANUP_INTERVAL = 3600.0  # run cleanup_old_jobs every hour

        self._write_daemon_log()
        self._reload_statuses_if_changed()
        self._write_readiness()

        logger.info(
            "BackendPoller started: deployment_id=%s interval=%.1fs parked=%s",
            self._deployment_id,
            self._current_interval,
            not self._deployment_in_use(),
        )
        status_watcher = asyncio.create_task(
            self._watch_status_file(),
            name="neo-thread-status-watcher",
        )
        await self._replay_pending_responses()

        while self._running:
            self._reload_statuses_if_changed()
            if not self._deployment_in_use() and not self._draining:
                # Parked — no v2/poll while the deployment has no RUNNING thread.
                self._write_readiness()
                await asyncio.sleep(self._park_tick)
                continue

            got_commands = False
            recently_active = (time.monotonic() - last_command_time) < 60
            try:
                # During active execution use wait_time=1 so the poll returns quickly
                # after the backend queues the next command (reduces worst-case per-file
                # latency from ~5s to ~1s). Idle: use full POLL_WAIT_TIME (less traffic).
                wait_time = 1 if recently_active else POLL_WAIT_TIME
                got_commands = await self._poll(wait_time=wait_time)
                if got_commands:
                    last_command_time = time.monotonic()
                    empty_streak = 0
                else:
                    empty_streak += 1
                    if self._draining:
                        self._drain_remaining -= 1
                        if self._drain_remaining <= 0:
                            self._draining = False
                            logger.info("Drain complete — parking v2/poll")
                    elif (
                        self._deployment_in_use()
                        and empty_streak >= self._empty_streak_before_status
                    ):
                        await self._confirm_running_thread_statuses()
                        empty_streak = 0
                        if not self._deployment_in_use() and not self._draining:
                            self._begin_drain()
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

            if self._running and not got_commands:
                if recently_active or self._draining:
                    # Small yield so the event loop can process cancellation/signals.
                    await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(self._current_interval)

        self._running = False
        status_watcher.cancel()
        try:
            await status_watcher
        except asyncio.CancelledError:
            pass
        logger.info("BackendPoller stopped")

    def stop(self) -> None:
        self._running = False

    def set_thread_status(self, thread_id: str, status: str) -> None:
        was_in_use = self._deployment_in_use()
        self._thread_statuses[thread_id] = status
        write_thread_status(thread_id, status)
        self._status_mtime = thread_statuses_mtime()
        if status in _TERMINAL_STATUSES:
            self._handlers._job_manager.terminate_thread_jobs(thread_id)
            release_workspace(thread_id=thread_id)
        if self._deployment_in_use():
            self._draining = False
            self._drain_remaining = 0
        elif was_in_use:
            self._begin_drain()

    def register_thread_workspace(self, thread_id: str, workspace: str) -> None:
        """Register workspace for a new thread immediately after task submission.

        Must be called right after init_chat returns thread_id — before any
        poll commands arrive — so write_code uses the correct local path.
        The shared dict is also read by ActionHandlers, so no extra wiring needed.
        """
        self._thread_workspaces[thread_id] = workspace
        self._save_thread_workspaces({thread_id})
        logger.info("Registered workspace for thread %s: %s", thread_id, workspace)

    def register_thread_wrapper(self, thread_id: str, wrapper: str) -> None:
        """Pre-seed a Neo project-slug for this thread.

        Append-if-new: the daemon also learns wrappers lazily from absolute
        container paths it sees in subsequent commands (see
        ActionHandlers._record_wrapper). Both sources accumulate into the same
        list so multi-alias projects (e.g. internal slug
        ``rag_system_langchain_0937`` plus a plan-text slug like
        ``kimi-rag-api``) all get stripped from shell text.

        Must be called right after init_chat returns thread_id — before any
        poll commands arrive — to close the race where Neo's first action is
        a relative `mkdir -p <slug>/...` and the wrapper isn't recorded yet.
        Safe to call with empty/None wrapper (no-op).
        """
        if not wrapper:
            return
        slugs = self._thread_wrappers.setdefault(thread_id, [])
        if wrapper in slugs:
            return
        slugs.append(wrapper)
        self._save_thread_workspaces({thread_id})
        logger.info("Registered wrapper for thread %s: %r (now %r)", thread_id, wrapper, slugs)

    def forget_thread(self, thread_id: str) -> None:
        """Evict a thread's workspace mapping and cached status.

        Called from _stop_task so that permanently-stopped threads don't
        accumulate in thread-workspaces.json. Safe to call for unknown
        thread IDs (no-op).
        """
        removed_workspace = self._thread_workspaces.pop(thread_id, None)
        removed_wrappers = self._thread_wrappers.pop(thread_id, None)
        # Keep _thread_statuses (TERMINATED) so the live daemon still rejects
        # leftover commands. Workspace eviction is the only job here.
        if removed_workspace is not None or removed_wrappers is not None:
            self._remove_persisted_thread(thread_id)
            logger.info(
                "Forgot thread %s (workspace=%s wrappers=%s)",
                thread_id, removed_workspace, removed_wrappers,
            )

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
        self._write_readiness()
        if not commands:
            return False
        logger.info("Received %d command(s)", len(commands))
        # Preserve backend order within a thread while retaining concurrency
        # across independent threads.
        groups: dict[str, list[dict[str, Any]]] = {}
        for index, command in enumerate(commands):
            thread_id = command.get("thread_id")
            key = str(thread_id) if thread_id else f"__unthreaded__:{index}"
            groups.setdefault(key, []).append(command)

        async def process_group(group: list[dict[str, Any]]) -> None:
            for command in group:
                await self._process_command(command)

        await asyncio.gather(*(process_group(group) for group in groups.values()))
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
        claim, prior_response = claim_command(deployment_id, command)
        if claim != "execute":
            response = prior_response or {
                "request_id": request_id,
                "status": "error",
                "error": f"COMMAND_{claim.upper()}",
            }
            response.setdefault("sandbox_id", deployment_id)
            if thread_id:
                response.setdefault("thread_id", thread_id)
            if response_queue:
                response["response_queue_name"] = response_queue
            await self._safe_send(response)
            return

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
            self._persist_result(command, error_resp, deployment_id)
            await self._safe_send(error_resp)
            return

        # If this thread has no registered workspace, reload from disk —
        # covers the case where a daemon restart lost in-memory state.
        # NOTE: never fall back to command["workspace"] — that is the backend's
        # container path (e.g. /app/project), not the local path. Using it would
        # pass _is_allowed_path checks and write files to /app/project directly.
        workspace_source = "memory"
        if thread_id and thread_id not in self._thread_workspaces:
            for _ in range(5):
                fresh = BackendPoller._load_thread_workspaces()
                if thread_id in fresh:
                    self._thread_workspaces.update(fresh)
                    workspace_source = "disk-reload"
                    break
                await asyncio.sleep(0.1)
            else:
                workspace_source = "missing"
                logger.warning("No workspace registered for thread %s", thread_id)

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

        self._persist_result(command, response, deployment_id)
        await self._safe_send(response)

    async def _safe_send(self, response: dict[str, Any]) -> None:
        delay = 0.5
        for attempt in range(1, 4):
            try:
                await self._client.send_response(self._deployment_id, response)
                mark_delivered(
                    str(response.get("sandbox_id") or self._deployment_id),
                    str(response.get("request_id") or ""),
                )
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

    def _persist_result(
        self,
        command: dict[str, Any],
        response: dict[str, Any],
        deployment_id: str,
    ) -> None:
        request_id = str(response.get("request_id") or "")
        workspace = None
        thread_id = command.get("thread_id")
        if isinstance(thread_id, str):
            workspace = self._thread_workspaces.get(thread_id)
        try:
            append_evidence(deployment_id, command, response, workspace)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not append execution evidence: %s", exc)
        complete_command(deployment_id, request_id, response)

    async def _replay_pending_responses(self) -> None:
        for response in pending_responses(self._deployment_id):
            await self._safe_send(response)

    def _should_accept(self, thread_id: str) -> bool:
        status = self._thread_statuses.get(thread_id)
        if status is None:
            return True  # no status tracked yet — allow (backwards compat)
        if status in _ALWAYS_ACCEPTED:
            return True
        if self._draining and status in _DRAIN_ACCEPTED:
            return True
        return False

    def _deployment_in_use(self) -> bool:
        return any_running(self._thread_statuses)

    def _begin_drain(self) -> None:
        self._draining = True
        self._drain_remaining = self._drain_empty
        logger.info(
            "Starting v2/poll drain (%d empty polls) before park",
            self._drain_remaining,
        )

    def _reload_statuses_if_changed(self) -> None:
        """Pick up pause/stop/resume written by the thin-client MCP process."""
        mtime = thread_statuses_mtime()
        if mtime == self._status_mtime:
            return
        was_in_use = self._deployment_in_use()
        previous = self._thread_statuses
        self._thread_statuses = load_thread_statuses()
        self._status_mtime = mtime
        for tid, status in self._thread_statuses.items():
            if status in _TERMINAL_STATUSES and previous.get(tid) != status:
                self._handlers._job_manager.terminate_thread_jobs(tid)
        now_in_use = self._deployment_in_use()
        if now_in_use:
            self._draining = False
            self._drain_remaining = 0
        elif was_in_use:
            self._begin_drain()
        self._write_readiness()

    def _write_readiness(self) -> None:
        """Local handshake document — written on the loop, not after a backend poll."""
        submission_intents = sorted(
            thread_id
            for thread_id, status in self._thread_statuses.items()
            if thread_id.startswith("__submission__:") and status == "RUNNING"
        )
        replace_json_object(
            deployment_ready_file(self._deployment_id),
            {
                "deployment_id": self._deployment_id,
                "pid": os.getpid(),
                "impl": "python",
                "version": package_version(),
                "executable": sys.executable,
                "observed_at": time.time(),
                "submission_intents": submission_intents,
            },
        )

    async def _watch_status_file(self) -> None:
        """Observe thin-client stop requests even while a blocking command runs.

        Heartbeat is written every tick so a long handler cannot freeze readiness.
        """
        while self._running:
            self._reload_statuses_if_changed()
            self._write_readiness()
            await asyncio.sleep(min(max(self._park_tick / 4, 0.05), 0.5))

    async def _confirm_running_thread_statuses(self) -> None:
        """Slow status GET for threads we still think are RUNNING.

        Detects backend-driven WAITING_FOR_FEEDBACK / COMPLETED without a
        second tight v2/poll loop. Transient errors leave the local status
        unchanged so we do not park on a blip.
        """
        running = [
            tid for tid, status in self._thread_statuses.items()
            if status == "RUNNING" and not tid.startswith("__submission__:")
        ]
        for tid in running:
            try:
                data = await self._client.get_thread_status(tid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Status confirm failed for %s: %s", tid, exc)
                continue
            if not isinstance(data, dict):
                continue
            new_status = data.get("status")
            if isinstance(new_status, str) and new_status and new_status != "RUNNING":
                logger.info(
                    "Thread %s left RUNNING (%s) — updating local status",
                    tid, new_status,
                )
                self.set_thread_status(tid, new_status)

    def _write_daemon_log(self) -> None:
        """Append a startup entry to ~/.neo/daemon/daemon.log.

        Format matches the VS Code extension's DaemonLogger (Logger.ts):
            [<ISO timestamp>] [INFO] <message> <meta json>

        Both ``deploymentId`` and ``sandboxId`` are emitted — the former is
        canonical, the latter kept for back-compat with older readers.
        """
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
             f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        meta = json.dumps({
            "deploymentId": self._deployment_id,
            "sandboxId": self._deployment_id,
            "source": "neo-mcp",
        })
        line = f"[{ts}] [INFO] BackendPoller started {meta}\n"
        try:
            with open(DAEMON_LOG, "a") as fh:
                fh.write(line)
        except OSError as exc:
            logger.warning("Could not write daemon log: %s", exc)

    def _save_thread_workspaces(self, thread_ids: Optional[set[str]] = None) -> None:
        _THREAD_WORKSPACE_TTL_DAYS: float = float(os.environ.get("NEO_THREAD_WORKSPACES_TTL_SECONDS", 7 * 24 * 60 * 60)) / 86400
        _THREAD_WORKSPACE_MAX: int = int(os.environ.get("NEO_THREAD_WORKSPACES_MAX", 500))
        try:
            now = datetime.now(timezone.utc)
            cutoff = now.timestamp() - _THREAD_WORKSPACE_TTL_DAYS * 86400

            def mutate(data: dict[str, Any]) -> None:
                # Merge only this process's known entries, preserving threads
                # concurrently registered by another MCP client.
                selected = (
                    self._thread_workspaces.items()
                    if thread_ids is None
                    else (
                        (tid, self._thread_workspaces.get(tid, ""))
                        for tid in thread_ids
                    )
                )
                for tid, ws in selected:
                    if not ws:
                        continue
                    previous = data.get(tid)
                    previous_ws = previous.get("workspace") if isinstance(previous, dict) else (
                        previous if isinstance(previous, str) else None
                    )
                    previous_ts = self._workspace_timestamp(previous)
                    explicitly_registered = thread_ids is not None
                    ts = (
                        now.timestamp()
                        if explicitly_registered
                        else (
                            previous_ts
                            if previous_ws == ws and previous_ts
                            else now.timestamp()
                        )
                    )
                    entry: dict[str, Any] = {
                        "workspace": ws,
                        "updated_at": int(ts),
                    }
                    wrappers = self._thread_wrappers.get(tid)
                    if wrappers:
                        entry["wrappers"] = list(wrappers)
                    elif isinstance(previous, dict) and previous.get("wrappers"):
                        entry["wrappers"] = previous["wrappers"]
                    data[tid] = entry

                ranked: list[tuple[str, dict[str, Any], float]] = []
                for tid, value in data.items():
                    if isinstance(value, str):
                        value = {"workspace": value, "updated_at": int(now.timestamp())}
                    if not isinstance(value, dict) or not value.get("workspace"):
                        continue
                    ts = self._workspace_timestamp(value)
                    if ts >= cutoff:
                        ranked.append((tid, value, ts))
                ranked.sort(key=lambda row: row[2])
                kept = ranked[-_THREAD_WORKSPACE_MAX:]
                data.clear()
                data.update({tid: value for tid, value, _ in kept})

            update_json_object(THREAD_WORKSPACES_FILE, mutate)
        except OSError as exc:
            logger.warning("Could not save thread workspaces: %s", exc)

    def _remove_persisted_thread(self, thread_id: str) -> None:
        try:
            update_json_object(
                THREAD_WORKSPACES_FILE,
                lambda data: data.pop(thread_id, None),
            )
        except OSError as exc:
            logger.warning("Could not remove thread workspace: %s", exc)

    @staticmethod
    def _workspace_timestamp(value: Any) -> float:
        if not isinstance(value, dict):
            return 0.0
        updated = value.get("updated_at")
        if isinstance(updated, (int, float)):
            return float(updated)
        if isinstance(updated, str):
            try:
                return datetime.fromisoformat(updated.rstrip("Z")).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    @staticmethod
    def _load_thread_workspaces() -> dict[str, str]:
        try:
            raw = read_json_object(THREAD_WORKSPACES_FILE)
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

    @staticmethod
    def _load_thread_wrappers() -> dict[str, list[str]]:
        """Read per-thread wrapper-slug lists from the same JSON file.

        Tolerant of:
          - Missing file → {}
          - Legacy entries (string-valued workspace, no wrappers field) → []
          - Single-string ``wrappers`` field (defensive) → wrapped in a list
        """
        try:
            raw = read_json_object(THREAD_WORKSPACES_FILE)
            result: dict[str, list[str]] = {}
            for tid, val in raw.items():
                if not isinstance(val, dict):
                    continue
                wrappers = val.get("wrappers")
                if isinstance(wrappers, str) and wrappers:
                    result[tid] = [wrappers]
                elif isinstance(wrappers, list):
                    result[tid] = [s for s in wrappers if isinstance(s, str) and s]
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load thread wrappers: %s", exc)
            return {}
