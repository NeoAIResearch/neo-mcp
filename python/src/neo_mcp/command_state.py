"""Durable command deduplication, response outbox, and local evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .ipc_state import read_json_object, update_json_object
from .paths import COMMAND_STATE_FILE, EXECUTION_EVIDENCE_FILE

_MAX_COMMANDS = int(os.environ.get("NEO_COMMAND_STATE_MAX", "5000"))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def _key(deployment_id: str, request_id: str) -> str:
    return hashlib.sha256(f"{deployment_id}\0{request_id}".encode()).hexdigest()


def claim_command(
    deployment_id: str,
    command: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Return execute, cached, conflict, or interrupted."""
    request_id = str(command.get("request_id") or "")
    if not request_id:
        return "execute", None
    key = _key(deployment_id, request_id)
    fingerprint_source = dict(command)
    # Delivery routing may legitimately change on backend redelivery and is
    # not part of the side effect's identity.
    fingerprint_source.pop("response_queue_name", None)
    fingerprint = hashlib.sha256(_canonical(fingerprint_source)).hexdigest()
    result: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> None:
        existing = data.get(key)
        if isinstance(existing, dict):
            if existing.get("fingerprint") != fingerprint:
                result.update({
                    "outcome": "conflict",
                    "response": {
                        "request_id": request_id,
                        "status": "error",
                        "error": "REQUEST_ID_REUSED_WITH_DIFFERENT_COMMAND",
                    },
                })
                return
            if existing.get("state") in {"COMPLETED", "DELIVERED"}:
                result.update({
                    "outcome": "cached",
                    "response": existing.get("response"),
                })
                return
            result.update({
                "outcome": "interrupted",
                "response": {
                    "request_id": request_id,
                    "status": "error",
                    "error": "COMMAND_OUTCOME_UNKNOWN_AFTER_INTERRUPTION",
                },
            })
            return
        data[key] = {
            "deployment_id": deployment_id,
            "request_id": request_id,
            "thread_id": command.get("thread_id"),
            "fingerprint": fingerprint,
            "state": "RECEIVED",
            "received_at": time.time(),
        }
        result["outcome"] = "execute"
        _prune(data)

    update_json_object(COMMAND_STATE_FILE, mutate)
    return str(result.get("outcome", "execute")), result.get("response")


def complete_command(
    deployment_id: str,
    request_id: str,
    response: dict[str, Any],
) -> None:
    if not request_id:
        return
    key = _key(deployment_id, request_id)

    def mutate(data: dict[str, Any]) -> None:
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = {
                "deployment_id": deployment_id,
                "request_id": request_id,
                "received_at": time.time(),
            }
            data[key] = entry
        entry.update({
            "state": "COMPLETED",
            "response": response,
            "completed_at": time.time(),
        })
        _prune(data)

    update_json_object(COMMAND_STATE_FILE, mutate)


def mark_delivered(deployment_id: str, request_id: str) -> None:
    if not request_id:
        return
    key = _key(deployment_id, request_id)

    def mutate(data: dict[str, Any]) -> None:
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("state") == "COMPLETED":
            entry["state"] = "DELIVERED"
            entry["delivered_at"] = time.time()

    update_json_object(COMMAND_STATE_FILE, mutate)


def pending_responses(deployment_id: str) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for entry in read_json_object(COMMAND_STATE_FILE).values():
        if (
            isinstance(entry, dict)
            and entry.get("deployment_id") == deployment_id
            and entry.get("state") == "COMPLETED"
            and isinstance(entry.get("response"), dict)
        ):
            pending.append(entry["response"])
    return pending


def append_evidence(
    deployment_id: str,
    command: dict[str, Any],
    response: dict[str, Any],
    workspace: str | None,
) -> dict[str, Any]:
    """Append a credential-minimizing, hash-chained observation."""
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    record: dict[str, Any] = {
        "schema_version": 1,
        "provenance": "local_daemon_observed",
        "timestamp": time.time(),
        "deployment_id": deployment_id,
        "thread_id": command.get("thread_id"),
        "request_id": command.get("request_id"),
        "action": command.get("action"),
        "command_sha256": hashlib.sha256(
            str(command.get("command") or "").encode()
        ).hexdigest() if command.get("command") is not None else None,
        "status": response.get("status"),
        "exit_code": data.get("exit_code"),
        "timed_out": data.get("timed_out"),
        "stdout_sha256": data.get("stdout_sha256") or _text_hash(data.get("stdout")),
        "stderr_sha256": data.get("stderr_sha256") or _text_hash(data.get("stderr")),
        "file_before_sha256": data.get("before_sha256"),
        "file_after_sha256": data.get("after_sha256"),
        "workspace": workspace,
    }
    file_path = data.get("file_path")
    if isinstance(file_path, str):
        record["file"] = _file_observation(Path(file_path), workspace)
    return _append_hash_chained(record)


def read_evidence(thread_id: str, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    try:
        lines = EXECUTION_EVIDENCE_FILE.read_text().splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("thread_id") == thread_id:
            records.append(record)
            if len(records) >= limit:
                break
    records.reverse()
    return records


def _append_hash_chained(record: dict[str, Any]) -> dict[str, Any]:
    path = EXECUTION_EVIDENCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        previous_hash: str | None = None
        try:
            with path.open("rb") as source:
                for line in source:
                    if line.strip():
                        previous_hash = json.loads(line).get("record_hash")
        except (OSError, json.JSONDecodeError, AttributeError):
            previous_hash = None
        record["previous_hash"] = previous_hash
        record_hash = hashlib.sha256(_canonical(record)).hexdigest()
        record["record_hash"] = record_hash
        with path.open("ab") as target:
            target.write(_canonical(record) + b"\n")
            target.flush()
            os.fsync(target.fileno())
        return record
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _file_observation(path: Path, workspace: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path)}
    try:
        resolved = path.resolve()
        if workspace is not None:
            ws = Path(workspace).resolve()
            if resolved != ws and ws not in resolved.parents:
                return {**result, "error": "outside_workspace"}
        content = resolved.read_bytes()
        result.update({
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    except OSError as exc:
        result["error"] = type(exc).__name__
    return result


def _text_hash(value: Any) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None


def _prune(data: dict[str, Any]) -> None:
    if len(data) <= _MAX_COMMANDS:
        return
    ranked = sorted(
        data.items(),
        key=lambda item: float(item[1].get("received_at", 0))
        if isinstance(item[1], dict) else 0,
    )
    for key, _ in ranked[: len(data) - _MAX_COMMANDS]:
        data.pop(key, None)
