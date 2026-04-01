"""Async HTTP client for the Neo backend API.

Mirrors BackendClient.ts:
  - poll_deployment    → GET  /v2/poll/{dep_id}?max_messages=N&wait_time=N
  - send_response      → POST /v2/poll/response
  - init_chat          → POST /v2/thread/init-chat-direct
  - get_thread_status  → GET  /v2/thread/status/{thread_id}
  - get_thread_messages→ GET  /v2/thread/thread-messages
  - send_feedback      → POST /v2/thread/feedback/{thread_id}
  - control_thread     → POST /v2/thread/control/{thread_id}
  - stop_thread        → DELETE /v2/thread/cleanup-direct/{thread_id}
"""

import logging
from typing import Any, Optional

import httpx

from .config import (
    API_URL,
    POLL_MAX_MESSAGES,
    POLL_TIMEOUT,
    POLL_WAIT_TIME,
    REQUEST_TIMEOUT,
)

logger = logging.getLogger(__name__)


class BackendClient:
    def __init__(self, auth_token: str, base_url: str = API_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token

    def update_token(self, token: str) -> None:
        self._auth_token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Daemon-side: poll + respond
    # ------------------------------------------------------------------

    async def poll_deployment(
        self,
        deployment_id: str,
        max_messages: int = POLL_MAX_MESSAGES,
        wait_time: int = POLL_WAIT_TIME,
        thread_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Long-poll the backend for pending commands.

        Returns a list of BackendCommand dicts (may be empty).
        Raises RuntimeError on unrecoverable errors (UNAUTHORIZED,
        DEPLOYMENT_NOT_FOUND).
        """
        url = (
            f"{self._base_url}/v2/poll/{deployment_id}"
            f"?max_messages={max_messages}&wait_time={wait_time}"
        )
        if thread_id:
            url += f"&thread_id={thread_id}"

        logger.debug("Polling backend: %s", url)

        async with httpx.AsyncClient(timeout=POLL_TIMEOUT) as client:
            try:
                resp = await client.get(url, headers=self._headers())
            except httpx.TimeoutException as exc:
                raise RuntimeError(f"Poll timeout: {exc}") from exc
            except httpx.RequestError as exc:
                raise RuntimeError(f"Poll network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if resp.status_code == 404:
            raise RuntimeError("DEPLOYMENT_NOT_FOUND")
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return data["messages"]
        return []

    async def send_response(
        self, deployment_id: str, response: dict[str, Any]
    ) -> None:
        """POST a command response back to the backend."""
        response.setdefault("sandbox_id", deployment_id)
        url = f"{self._base_url}/v2/poll/response"

        logger.debug(
            "Sending response: request_id=%s status=%s",
            response.get("request_id"),
            response.get("status"),
        )

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.post(url, json=response, headers=self._headers())
            except httpx.RequestError as exc:
                raise RuntimeError(f"send_response network error: {exc}") from exc

        if not resp.is_success:
            raise RuntimeError(f"send_response HTTP {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------------
    # MCP tool-side: thread lifecycle
    # ------------------------------------------------------------------

    async def init_chat(
        self,
        message: str,
        deployment_id: str,
        workspace: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /v2/thread/init-chat-direct — submit a new task."""
        url = f"{self._base_url}/v2/thread/init-chat-direct"
        payload: dict[str, Any] = {
            "message": message,
            "deployment_id": deployment_id,
            "deployment_type": "vscode",
        }
        if workspace:
            payload["workspace"] = workspace

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
            except httpx.RequestError as exc:
                raise RuntimeError(f"init_chat network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if not resp.is_success:
            body = resp.json() if resp.content else {}
            raise RuntimeError(body.get("error") or f"HTTP {resp.status_code}")

        return resp.json()

    async def get_thread_status(self, thread_id: str) -> dict[str, Any]:
        """GET /v2/thread/status/{thread_id}"""
        url = f"{self._base_url}/v2/thread/status/{thread_id}"

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.get(url, headers=self._headers())
            except httpx.RequestError as exc:
                raise RuntimeError(f"get_thread_status network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if resp.status_code == 404:
            raise RuntimeError("THREAD_NOT_FOUND")
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.json()

    async def get_thread_messages(
        self,
        thread_id: str,
        before: Optional[str] = None,
        after: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """GET /v2/thread/thread-messages"""
        params: dict[str, str] = {"thread_id": thread_id}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if limit is not None:
            params["limit"] = str(limit)

        url = f"{self._base_url}/v2/thread/thread-messages"

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.get(url, params=params, headers=self._headers())
            except httpx.RequestError as exc:
                raise RuntimeError(f"get_thread_messages network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if resp.status_code == 404:
            raise RuntimeError("THREAD_NOT_FOUND")
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.json()

    async def send_feedback(self, thread_id: str, message: str) -> None:
        """POST /v2/thread/feedback/{thread_id}"""
        url = f"{self._base_url}/v2/thread/feedback/{thread_id}"

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.post(
                    url, json={"input": message}, headers=self._headers()
                )
            except httpx.RequestError as exc:
                raise RuntimeError(f"send_feedback network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if not resp.is_success:
            body = resp.json() if resp.content else {}
            raise RuntimeError(body.get("error") or f"HTTP {resp.status_code}")

    async def control_thread(self, thread_id: str, signal: str) -> None:
        """POST /v2/thread/control/{thread_id} with signal PAUSE or RESUME."""
        url = f"{self._base_url}/v2/thread/control/{thread_id}"

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.post(
                    url, json={"signal": signal}, headers=self._headers()
                )
            except httpx.RequestError as exc:
                raise RuntimeError(f"control_thread network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if not resp.is_success:
            body = resp.json() if resp.content else {}
            raise RuntimeError(body.get("error") or f"HTTP {resp.status_code}")

    async def stop_thread(self, thread_id: str) -> None:
        """DELETE /v2/thread/cleanup-direct/{thread_id}"""
        url = f"{self._base_url}/v2/thread/cleanup-direct/{thread_id}?delete_remote_artifacts=false"

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.delete(url, headers=self._headers())
            except httpx.RequestError as exc:
                raise RuntimeError(f"stop_thread network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if not resp.is_success:
            body = resp.json() if resp.content else {}
            raise RuntimeError(body.get("error") or f"HTTP {resp.status_code}")
