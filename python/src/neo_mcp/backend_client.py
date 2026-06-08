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

Uses a single persistent httpx.AsyncClient with connection pooling so that
concurrent command handlers (poll, send_response × N, init_chat, …) reuse
TCP/TLS connections instead of opening a fresh one for every call.
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

# Connection pool limits — enough for concurrent command handlers without
# exhausting local file descriptors.
# Evict idle keep-alive connections after 30 s. Prevents stale connections
# accumulating in long-running processes (backend closes idle connections
# after ~60 s, so we evict first to avoid RemoteProtocolError / PoolTimeout).
_POOL_LIMITS = httpx.Limits(
    max_connections=40,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)


class BackendClient:
    def __init__(self, auth_token: str, base_url: str = API_URL) -> None:
        self._base_url = base_url.rstrip("/")
        # Strip surrounding whitespace defensively — a trailing space in the
        # token (e.g. from a sloppy MCP env-block in ~/.claude.json) becomes
        # ``Bearer <token> `` in the Authorization header, which httpx rejects
        # as ``Illegal header value``. Without this the daemon polls forever
        # and the only symptom is repeating poll errors in the log.
        self._auth_token = (auth_token or "").strip()
        self._http = httpx.AsyncClient(
            limits=_POOL_LIMITS,
            timeout=REQUEST_TIMEOUT,
        )

    def update_token(self, token: str) -> None:
        self._auth_token = (token or "").strip()

    async def aclose(self) -> None:
        """Close the underlying connection pool. Call on shutdown."""
        await self._http.aclose()

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

        try:
            resp = await self._http.get(url, headers=self._headers(), timeout=POLL_TIMEOUT)
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

        try:
            resp = await self._http.post(url, json=response, headers=self._headers())
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
        wrapper_hint: Optional[str] = None,
        byok_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """POST /v2/thread/init-chat-direct — submit a new task.

        ``wrapper_hint`` (optional) is the project-slug Neo should treat as the
        per-thread wrapper. Forwarded to the backend so it can echo the slug
        in subsequent commands; the daemon also seeds it locally via
        ``BackendPoller.register_thread_wrapper`` so wrapper-stripping is in
        place before the very first command arrives. Backends that don't know
        the field ignore it — this is forward-compat only.

        ``byok_headers`` (optional) carries the ``x-llm-*`` BYOK headers so the
        backend runs the orchestrator on the user's own LLM key. Attached to
        exactly init-chat-direct and feedback.
        """
        url = f"{self._base_url}/v2/thread/init-chat-direct"
        headers = {**self._headers(), **(byok_headers or {})}
        payload: dict[str, Any] = {
            "message": message,
            "deployment_id": deployment_id,
            "deployment_type": "vscode",
        }
        if workspace:
            payload["workspace"] = workspace
        if wrapper_hint:
            payload["wrapper_hint"] = wrapper_hint

        # Retry once on RemoteProtocolError or TimeoutException — both can be
        # transient: stale keep-alive connections (server already closed them)
        # or slow cold-starts. httpx auto-retries idempotent methods (GET) but
        # not POST, so we do it ourselves. Second attempt always gets a fresh
        # connection.
        resp: Optional[httpx.Response] = None
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = await self._http.post(url, json=payload, headers=headers)
                last_exc = None
                break
            except (httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == 0:
                    logger.debug(
                        "init_chat: %s on attempt 1 (%s), retrying",
                        type(exc).__name__,
                        str(exc) or "<no message>",
                    )
                    continue
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"init_chat network error ({type(exc).__name__}): "
                    f"{str(exc) or '<no message>'}"
                ) from exc

        if last_exc is not None:
            detail = str(last_exc) or "<no message>"
            if isinstance(last_exc, httpx.TimeoutException):
                raise RuntimeError(
                    f"init_chat timed out after {REQUEST_TIMEOUT}s "
                    f"({type(last_exc).__name__}: {detail})"
                ) from last_exc
            raise RuntimeError(
                f"init_chat network error ({type(last_exc).__name__}): {detail}"
            ) from last_exc

        assert resp is not None
        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if not resp.is_success:
            body = resp.json() if resp.content else {}
            raise RuntimeError(body.get("error") or f"HTTP {resp.status_code}")

        return resp.json()

    async def get_thread_status(self, thread_id: str) -> dict[str, Any]:
        """GET /v2/thread/status/{thread_id}"""
        url = f"{self._base_url}/v2/thread/status/{thread_id}"

        try:
            resp = await self._http.get(url, headers=self._headers())
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

        try:
            resp = await self._http.get(url, params=params, headers=self._headers())
        except httpx.RequestError as exc:
            raise RuntimeError(f"get_thread_messages network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if resp.status_code == 404:
            raise RuntimeError("THREAD_NOT_FOUND")
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.json()

    async def send_feedback(
        self,
        thread_id: str,
        message: str,
        byok_headers: Optional[dict[str, str]] = None,
    ) -> None:
        """POST /v2/thread/feedback/{thread_id}

        ``byok_headers`` (optional) carries the ``x-llm-*`` BYOK headers — same
        as init_chat — so feedback turns also run on the user's own LLM key.
        """
        url = f"{self._base_url}/v2/thread/feedback/{thread_id}"
        headers = {**self._headers(), **(byok_headers or {})}

        try:
            resp = await self._http.post(
                url, json={"input": message}, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"send_feedback timed out: {exc}") from exc
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

        try:
            resp = await self._http.post(
                url, json={"signal": signal}, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"control_thread timed out: {exc}") from exc
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

        try:
            resp = await self._http.delete(url, headers=self._headers())
        except httpx.RequestError as exc:
            raise RuntimeError(f"stop_thread network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if not resp.is_success:
            body = resp.json() if resp.content else {}
            raise RuntimeError(body.get("error") or f"HTTP {resp.status_code}")

    async def fetch_byok_providers(self) -> list[dict[str, Any]]:
        """GET /v2/thread/fetch-byok-providers — BYOK provider/model catalog.

        Each row is
        ``{provider, supported_models, base_url?, test_url?}``. Returns [] on a
        non-list body so callers can fall back to the hardcoded model lists.
        """
        url = f"{self._base_url}/v2/thread/fetch-byok-providers"
        try:
            resp = await self._http.get(url, headers=self._headers())
        except httpx.RequestError as exc:
            raise RuntimeError(f"fetch_byok_providers network error: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError("UNAUTHORIZED")
        if not resp.is_success:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return data if isinstance(data, list) else []
