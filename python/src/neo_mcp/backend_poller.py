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
from datetime import datetime, timezone
from typing import Any, Optional

from .action_handlers import ActionHandlers
from .backend_client import BackendClient
from .config import POLL_BACKOFF_FACTOR, POLL_BASE_INTERVAL, POLL_MAX_INTERVAL
from .paths import DAEMON_DIR, DAEMON_LOG, THREAD_WORKSPACES_FILE

logger = logging.getLogger(__name__)

_ACCEPTED_STATUSES = frozenset({"RUNNING", "PAUSED"})


class BackendPoller:
    """Runs as an asyncio background task."""

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Async entry point — run until cancelled or stop() is called."""
        self._running = True
        self._consecutive_errors = 0
        self._current_interval = POLL_BASE_INTERVAL

        self._write_daemon_log()

        logger.info(
            "BackendPoller started: deployment_id=%s interval=%.1fs",
            self._deployment_id,
            self._current_interval,
        )

        while self._running:
            try:
                await self._poll()
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

            if self._running:
                await asyncio.sleep(self._current_interval)

        self._running = False
        logger.info("BackendPoller stopped")

    def stop(self) -> None:
        self._running = False

    def set_thread_status(self, thread_id: str, status: str) -> None:
        self._thread_statuses[thread_id] = status

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

    async def _poll(self) -> None:
        commands = await self._client.poll_deployment(self._deployment_id)
        if not commands:
            return
        logger.info("Received %d command(s)", len(commands))
        for command in commands:
            await self._process_command(command)

    async def _process_command(self, command: dict[str, Any]) -> None:
        request_id = command.get("request_id", "")
        action = command.get("action", "")
        thread_id = command.get("thread_id")
        deployment_id = command.get("deployment_id") or self._deployment_id
        response_queue = command.get("response_queue_name")

        logger.info(
            "Command: action=%s request_id=%s thread_id=%s", action, request_id, thread_id
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

        # Update thread workspace mapping if present
        if thread_id and command.get("workspace"):
            self._thread_workspaces[thread_id] = command["workspace"]
            self._save_thread_workspaces()

        try:
            response = await self._handlers.handle_command(command)
        except Exception as exc:  # noqa: BLE001
            logger.error("Handler failed: %s", exc, exc_info=True)
            response = {"request_id": request_id, "status": "error", "error": str(exc)}

        # Attach routing fields required by backend
        response["sandbox_id"] = deployment_id
        if thread_id:
            response["thread_id"] = thread_id
        if response_queue:
            response["response_queue_name"] = response_queue

        await self._safe_send(response)

    async def _safe_send(self, response: dict[str, Any]) -> None:
        try:
            await self._client.send_response(self._deployment_id, response)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send response for %s: %s", response.get("request_id"), exc)

    def _should_accept(self, thread_id: str) -> bool:
        status = self._thread_statuses.get(thread_id)
        if status is None:
            return True  # no status tracked yet — allow (backwards compat)
        return status in _ACCEPTED_STATUSES

    def _write_daemon_log(self) -> None:
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"sandboxId": self._deployment_id, "source": "neo-mcp"})
        try:
            with open(DAEMON_LOG, "a") as fh:
                fh.write(entry + "\n")
        except OSError as exc:
            logger.warning("Could not write daemon log: %s", exc)

    def _save_thread_workspaces(self) -> None:
        try:
            data = {
                tid: {"workspace": ws, "updated_at": datetime.now(timezone.utc).isoformat()}
                for tid, ws in self._thread_workspaces.items()
            }
            THREAD_WORKSPACES_FILE.write_text(json.dumps(data, indent=2))
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
