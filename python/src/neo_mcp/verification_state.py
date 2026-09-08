"""Persisted local verification outcomes, separate from backend status."""

from __future__ import annotations

import time
from typing import Any

from .ipc_state import read_json_object, update_json_object
from .paths import VERIFICATION_RESULTS_FILE


def get_verification(thread_id: str) -> dict[str, Any] | None:
    value = read_json_object(VERIFICATION_RESULTS_FILE).get(thread_id)
    return value if isinstance(value, dict) else None


def set_verification(
    thread_id: str,
    status: str,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    value = {
        "verification_status": status,
        "verification_provenance": "local_daemon_observed",
        "verified_at": time.time(),
        "checks": checks,
    }

    def mutate(data: dict[str, Any]) -> None:
        data[thread_id] = value

    update_json_object(VERIFICATION_RESULTS_FILE, mutate)
    return value
