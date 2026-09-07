"""Regression coverage for Python MCP safety and lifecycle hardening."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
from functools import wraps
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types

from neo_mcp.action_handlers import ActionHandlers
from neo_mcp.backend_poller import BackendPoller
from neo_mcp.job_manager import JobManager


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return run


def _status_writer(thread_id: str, start: multiprocessing.synchronize.Event) -> None:
    from neo_mcp.thread_status import write_thread_status

    start.wait()
    write_thread_status(thread_id, "RUNNING")


def _workspace_writer(
    thread_id: str,
    workspace: str,
    start: multiprocessing.synchronize.Event,
) -> None:
    start.wait()
    handlers = ActionHandlers(JobManager(), workspace, {thread_id: workspace})
    poller = BackendPoller("dep", MagicMock(), handlers, handlers._thread_workspaces)
    poller.register_thread_workspace(thread_id, workspace)


def _guard_holder(
    deployment_id: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    from neo_mcp.server import _release_daemon_guard, _try_acquire_daemon_guard

    fd = _try_acquire_daemon_guard(deployment_id)
    if fd is None:
        return
    ready.set()
    release.wait(5)
    _release_daemon_guard(fd)


def _handlers(workspace: Path, thread_id: str = "thread-1") -> ActionHandlers:
    return ActionHandlers(
        JobManager(),
        str(workspace),
        {thread_id: str(workspace)},
    )


@async_test
async def test_list_files_rejects_relative_traversal_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling"
    workspace.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret")
    (workspace / "outside").symlink_to(sibling, target_is_directory=True)
    handlers = _handlers(workspace)

    for directory in ("../sibling", "outside"):
        result = await handlers.handle_command({
            "action": "list_files",
            "request_id": directory,
            "thread_id": "thread-1",
            "directory": directory,
        })
        assert result["status"] == "error"
        assert "secret.txt" not in json.dumps(result)


@async_test
async def test_blocking_subprocess_is_timed_and_output_capped(tmp_path: Path) -> None:
    import neo_mcp.job_manager as jobs

    handlers = _handlers(tmp_path)
    with patch.object(jobs, "JOB_MAX_RUNTIME", 0.15):
        result = await handlers.handle_command({
            "action": "run_subprocess",
            "request_id": "timeout",
            "thread_id": "thread-1",
            "command": "sleep 5",
            "detach": False,
        })
    assert result["status"] == "error"
    assert result["data"]["timed_out"] is True

    with patch.object(jobs, "MAX_LOG_BYTES", 256):
        result = await handlers.handle_command({
            "action": "run_subprocess",
            "request_id": "cap",
            "thread_id": "thread-1",
            "command": f"{sys.executable} -c \"print('x' * 4096)\"",
            "detach": False,
        })
    assert len(result["data"]["stdout"].encode()) <= 256
    assert result["data"]["stdout_truncated"] is True


@async_test
async def test_thread_job_termination_preserves_other_threads(tmp_path: Path) -> None:
    manager = JobManager()
    first = await manager.create_job("sleep 30", str(tmp_path), "first")
    second = await manager.create_job("sleep 30", str(tmp_path), "second")
    await asyncio.sleep(0.05)
    try:
        assert manager.terminate_thread_jobs("first") == 1
        for _ in range(50):
            first_logs = manager.get_job_logs(first)
            if first_logs and first_logs["exit_code"] is not None:
                break
            await asyncio.sleep(0.02)
        assert manager.get_job_logs(first)["exit_code"] is not None
        assert manager.get_job_logs(second)["exit_code"] is None
    finally:
        manager.terminate_job(first)
        manager.terminate_job(second)


@async_test
async def test_immediate_job_termination_reaches_terminal_state(tmp_path: Path) -> None:
    manager = JobManager()
    job_id = await manager.create_job("sleep 30", str(tmp_path), "thread")
    assert manager.terminate_job(job_id) is True
    await asyncio.sleep(0)
    logs = manager.get_job_logs(job_id)
    assert logs["exit_code"] == -1
    assert logs["completed_at"] is not None


@async_test
async def test_stop_task_terminates_its_local_jobs(tmp_path: Path) -> None:
    from neo_mcp.server import _stop_task

    manager = JobManager()
    handlers = ActionHandlers(
        manager,
        str(tmp_path),
        {"stopped-thread": str(tmp_path)},
    )
    poller = BackendPoller(
        "dep", MagicMock(), handlers, handlers._thread_workspaces
    )
    job_id = await manager.create_job("sleep 30", str(tmp_path), "stopped-thread")
    await asyncio.sleep(0.05)
    client = MagicMock()
    client.stop_thread = AsyncMock()
    await _stop_task(client, poller, {"thread_id": "stopped-thread"})
    for _ in range(50):
        if manager.get_job_logs(job_id)["exit_code"] is not None:
            break
        await asyncio.sleep(0.02)
    assert manager.get_job_logs(job_id)["exit_code"] is not None


@async_test
async def test_poller_orders_each_thread_but_overlaps_threads(tmp_path: Path) -> None:
    commands = [
        {"request_id": "a", "action": "noop", "thread_id": "one"},
        {"request_id": "b", "action": "noop", "thread_id": "one"},
        {"request_id": "c", "action": "noop", "thread_id": "two"},
    ]
    client = MagicMock()
    client.poll_deployment = AsyncMock(return_value=commands)
    handlers = _handlers(tmp_path)
    poller = BackendPoller("dep", client, handlers, handlers._thread_workspaces)
    a_started = asyncio.Event()
    c_started = asyncio.Event()
    release_a = asyncio.Event()
    order: list[str] = []

    async def process(command: dict) -> None:
        request_id = command["request_id"]
        order.append(f"start-{request_id}")
        if request_id == "a":
            a_started.set()
            await release_a.wait()
        elif request_id == "c":
            c_started.set()
        order.append(f"end-{request_id}")

    poller._process_command = process  # type: ignore[method-assign]
    task = asyncio.create_task(poller._poll())
    await asyncio.wait_for(a_started.wait(), 1)
    await asyncio.wait_for(c_started.wait(), 1)
    assert "start-b" not in order
    release_a.set()
    await task
    assert order.index("end-a") < order.index("start-b")


def test_cross_process_status_and_workspace_updates_survive(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    status_processes = [
        ctx.Process(target=_status_writer, args=(f"status-{i}", start))
        for i in range(8)
    ]
    workspace_processes = [
        ctx.Process(
            target=_workspace_writer,
            args=(f"workspace-{i}", str(tmp_path / f"ws-{i}"), start),
        )
        for i in range(8)
    ]
    processes = status_processes + workspace_processes
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    from neo_mcp.backend_poller import BackendPoller
    from neo_mcp.thread_status import load_thread_statuses

    assert set(load_thread_statuses()) >= {f"status-{i}" for i in range(8)}
    assert set(BackendPoller._load_thread_workspaces()) >= {
        f"workspace-{i}" for i in range(8)
    }


def test_workspace_change_refreshes_stale_timestamp(tmp_path: Path) -> None:
    from neo_mcp.paths import THREAD_WORKSPACES_FILE

    THREAD_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
    THREAD_WORKSPACES_FILE.write_text(json.dumps({
        "thread": {"workspace": "/old", "updated_at": 1},
    }))
    handlers = _handlers(tmp_path, "thread")
    poller = BackendPoller("dep", MagicMock(), handlers, handlers._thread_workspaces)
    poller.register_thread_workspace("thread", str(tmp_path))
    saved = json.loads(THREAD_WORKSPACES_FILE.read_text())["thread"]
    assert saved["workspace"] == str(tmp_path)
    assert saved["updated_at"] > 1


def test_same_workspace_registration_refreshes_stale_timestamp(tmp_path: Path) -> None:
    from neo_mcp.paths import THREAD_WORKSPACES_FILE

    THREAD_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
    THREAD_WORKSPACES_FILE.write_text(json.dumps({
        "thread": {"workspace": str(tmp_path), "updated_at": 1},
    }))
    handlers = _handlers(tmp_path, "thread")
    poller = BackendPoller("dep", MagicMock(), handlers, handlers._thread_workspaces)
    poller.register_thread_workspace("thread", str(tmp_path))
    saved = json.loads(THREAD_WORKSPACES_FILE.read_text())["thread"]
    assert saved["updated_at"] > 1


def test_only_one_daemon_guard_holder() -> None:
    from neo_mcp.server import _try_acquire_daemon_guard

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(target=_guard_holder, args=("same-deployment", ready, release))
    process.start()
    assert ready.wait(5)
    try:
        assert _try_acquire_daemon_guard("same-deployment") is None
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


@async_test
async def test_read_only_catalog_and_direct_call_enforcement(tmp_path: Path) -> None:
    from neo_mcp.server import build_server

    with patch.dict(os.environ, {"NEO_READ_ONLY": "true"}):
        server, client, _ = build_server("sk-v1-test", str(tmp_path))
        list_result = await server.request_handlers[mcp_types.ListToolsRequest](
            mcp_types.ListToolsRequest()
        )
        names = {tool.name for tool in list_result.root.tools}
        assert "neo_submit_task" not in names
        assert "neo_add_integration" not in names
        assert "neo_task_status" in names
        assert "neo_get_execution_evidence" in names
        assert "neo_verify_task" not in names

        call_result = await server.request_handlers[mcp_types.CallToolRequest](
            mcp_types.CallToolRequest(
                params=mcp_types.CallToolRequestParams(
                    name="neo_submit_task",
                    arguments={"message": "x", "workspace": str(tmp_path)},
                )
            )
        )
        assert call_result.root.isError is True
        await client.aclose()


@async_test
async def test_unknown_tool_sets_mcp_error_flag(tmp_path: Path) -> None:
    from neo_mcp.server import build_server

    server, client, _ = build_server("sk-v1-test", str(tmp_path))
    result = await server.request_handlers[mcp_types.CallToolRequest](
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="neo_does_not_exist",
                arguments={},
            )
        )
    )
    assert result.root.isError is True
    await client.aclose()


def test_missing_key_stdio_fails_fast(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("NEO_SECRET_KEY", None)
    env.pop("NEO_WORKSPACE_DIR", None)
    env.pop("NEO_DEPLOYMENT_ID", None)
    env["NEO_HOME"] = str(tmp_path / "neo-home")
    env["HOME"] = str(tmp_path / "home")
    source = Path(__file__).parents[1] / "src"
    env["PYTHONPATH"] = str(source)
    result = subprocess.run(
        [sys.executable, "-m", "neo_mcp"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
            timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "NEO_SECRET_KEY is required" in result.stderr
    assert result.stdout == ""


def test_stop_daemon_rejects_reused_unrelated_pid(tmp_path: Path) -> None:
    from neo_mcp import service

    pid_file = tmp_path / "neo-mcp.pid"
    pid_file.write_text("4242")
    signals: list[tuple[int, int]] = []
    with (
        patch.object(service, "_pid_alive", return_value=True),
        patch.object(service, "_pid_cmdline", return_value="/bin/sleep 100"),
        patch.object(service.os, "kill", side_effect=lambda pid, sig: signals.append((pid, sig))),
        patch.object(service, "PID_FILE", pid_file),
        patch.object(service, "LOCK_FILE", tmp_path / "missing.lock"),
        patch.object(service, "_deployment_pid_file", return_value=tmp_path / "missing.pid"),
    ):
        assert service.stop_daemon("deployment", timeout=0) == 0
    assert signals == []


@async_test
async def test_status_projection_is_bounded_and_truthful() -> None:
    from neo_mcp.server import _task_status

    client = MagicMock()
    client.get_thread_status = AsyncMock(return_value={
        "status": "WAITING_FOR_FEEDBACK",
        "current_plan": [{"description": "x" * 5000}] * 20,
        "completed_subtasks": [],
        "executor_activity": "still writing",
        "executor_activity_log": [f"{i}-" + ("x" * 1000) for i in range(20)],
        "unknown_large_field": "y" * 10000,
    })
    result = await _task_status(client, {"thread_id": "status-thread"})
    assert result["status"] == "WAITING_FOR_FEEDBACK"
    assert result["status_provenance"] == "backend_reported"
    assert result["verification_status"] == "NOT_RUN"
    assert "current_plan" not in result
    assert "completed_subtasks" not in result
    assert "unknown_large_field" not in result
    assert len(result["executor_activity_log"]) == 5
    assert all(len(item) <= 512 for item in result["executor_activity_log"])
    assert result["activity_entries_omitted"] == 15
    assert result["consistency_warnings"]
    assert len(json.dumps(result)) < 4096


@async_test
async def test_status_read_does_not_release_active_workspace_lease(tmp_path: Path) -> None:
    from neo_mcp.paths import WORKSPACE_LEASES_FILE
    from neo_mcp.server import _task_status
    from neo_mcp.workspace_leases import (
        acquire_workspace,
        bind_workspace,
        lease_for_workspace,
    )

    WORKSPACE_LEASES_FILE.unlink(missing_ok=True)
    acquired, _ = acquire_workspace(
        str(tmp_path),
        "10000000-0000-4000-8000-000000000011",
        "status-lease-deployment",
    )
    assert acquired
    bind_workspace(
        str(tmp_path),
        "10000000-0000-4000-8000-000000000011",
        "status-lease-thread",
    )

    client = MagicMock()
    client.get_thread_status = AsyncMock(return_value={
        "status": "COMPLETED",
        "executor_activity": "finished",
    })
    result = await _task_status(client, {"thread_id": "status-lease-thread"})
    assert result["status"] == "COMPLETED"
    lease = lease_for_workspace(str(tmp_path))
    assert lease is not None
    assert lease["thread_id"] == "status-lease-thread"


@async_test
async def test_backend_client_rejects_non_object_status() -> None:
    from neo_mcp.backend_client import BackendClient

    client = BackendClient("test")
    response = MagicMock(status_code=200, is_success=True)
    response.json.return_value = []
    client._http.get = AsyncMock(return_value=response)
    try:
        try:
            await client.get_thread_status("thread")
            raise AssertionError("expected malformed status failure")
        except RuntimeError as exc:
            assert "expected a JSON object" in str(exc)
    finally:
        await client.aclose()


@async_test
async def test_submission_reserves_workspace_and_registers_before_wake(tmp_path: Path) -> None:
    from neo_mcp.paths import WORKSPACE_LEASES_FILE
    from neo_mcp.server import _submit_task
    from neo_mcp.workspace_leases import release_workspace

    WORKSPACE_LEASES_FILE.unlink(missing_ok=True)
    client = MagicMock()
    client.init_chat = AsyncMock(return_value={"thread_id": "lease-thread"})
    poller = MagicMock()
    events: list[str] = []
    poller.register_thread_workspace.side_effect = lambda *_: events.append("workspace")
    poller.set_thread_status.side_effect = lambda *_: events.append("status")
    byok = MagicMock()
    byok.resolve_active_headers.return_value = ({}, None)
    first_id = "10000000-0000-4000-8000-000000000001"
    second_id = "10000000-0000-4000-8000-000000000002"
    with (
        patch("neo_mcp.server.build_context_prefix", new=AsyncMock(return_value="")),
        patch(
            "neo_mcp.server.IntegrationManager.format_integrations_xml",
            return_value="",
        ),
    ):
        first = await _submit_task(
            client, "dep", poller, str(tmp_path), byok,
            {"message": "one", "workspace": str(tmp_path), "submission_id": first_id},
        )
        second = await _submit_task(
            client, "dep", poller, str(tmp_path), byok,
            {"message": "two", "workspace": str(tmp_path), "submission_id": second_id},
        )
    assert first["thread_id"] == "lease-thread"
    assert events == ["workspace", "status"]
    assert second["code"] == "WORKSPACE_BUSY"
    assert second["owner_thread_id"] == "lease-thread"
    assert client.init_chat.await_count == 1
    release_workspace(thread_id="lease-thread")


@async_test
async def test_pre_submit_intent_wakes_poller_and_records_readiness(
    tmp_path: Path,
) -> None:
    from neo_mcp.ipc_state import read_json_object
    from neo_mcp.paths import deployment_ready_file

    deployment_id = "readiness-deployment"
    intent = "__submission__:10000000-0000-4000-8000-000000000009"
    ready_file = deployment_ready_file(deployment_id)
    ready_file.unlink(missing_ok=True)
    handlers = ActionHandlers(JobManager(), str(tmp_path), {})
    client = MagicMock()
    client.poll_deployment = AsyncMock(return_value=[])
    poller = BackendPoller(deployment_id, client, handlers, {})
    poller._thread_statuses = {intent: "RUNNING"}

    assert await poller._poll(wait_time=1) is False
    readiness = read_json_object(ready_file)
    assert readiness["deployment_id"] == deployment_id
    assert intent in readiness["submission_intents"]


@async_test
async def test_submit_waits_for_matching_poller_readiness(tmp_path: Path) -> None:
    from neo_mcp.ipc_state import replace_json_object
    from neo_mcp.paths import WORKSPACE_LEASES_FILE, deployment_ready_file
    from neo_mcp.server import _submit_task
    from neo_mcp.thread_status import load_thread_statuses
    from neo_mcp.workspace_leases import release_workspace

    deployment_id = "ready-submit-deployment"
    submission_id = "10000000-0000-4000-8000-000000000010"
    intent = f"__submission__:{submission_id}"
    WORKSPACE_LEASES_FILE.unlink(missing_ok=True)
    replace_json_object(
        deployment_ready_file(deployment_id),
        {
            "deployment_id": deployment_id,
            "submission_intents": [intent],
        },
    )
    client = MagicMock()
    client.init_chat = AsyncMock(return_value={"thread_id": "ready-thread"})
    poller = MagicMock()
    byok = MagicMock()
    byok.resolve_active_headers.return_value = ({}, None)
    with (
        patch("neo_mcp.server.build_context_prefix", new=AsyncMock(return_value="")),
        patch(
            "neo_mcp.server.IntegrationManager.format_integrations_xml",
            return_value="",
        ),
    ):
        result = await _submit_task(
            client,
            deployment_id,
            poller,
            str(tmp_path),
            byok,
            {
                "message": "ready",
                "workspace": str(tmp_path),
                "submission_id": submission_id,
            },
            require_poller_ready=True,
        )
    assert result["thread_id"] == "ready-thread"
    assert intent not in load_thread_statuses()
    poller.register_thread_workspace.assert_called_once()
    poller.set_thread_status.assert_called_once_with("ready-thread", "RUNNING")
    release_workspace(thread_id="ready-thread")


@async_test
async def test_init_chat_sends_idempotency_key_without_timeout_retry() -> None:
    import httpx

    from neo_mcp.backend_client import AmbiguousSubmissionError, BackendClient
    from neo_mcp.config import INIT_CHAT_TIMEOUT

    client = BackendClient("test")
    client._http.post = AsyncMock(side_effect=httpx.ReadTimeout(""))
    submission_id = "10000000-0000-4000-8000-000000000003"
    try:
        try:
            await client.init_chat("message", "dep", submission_id=submission_id)
            raise AssertionError("expected ambiguous submission")
        except AmbiguousSubmissionError as exc:
            assert submission_id in str(exc)
        assert client._http.post.await_count == 1
        assert client._http.post.await_args.kwargs["headers"]["Idempotency-Key"] == submission_id
        assert client._http.post.await_args.kwargs["timeout"] == INIT_CHAT_TIMEOUT
    finally:
        await client.aclose()


@async_test
async def test_command_dedup_outbox_and_evidence(tmp_path: Path) -> None:
    from neo_mcp.command_state import pending_responses, read_evidence
    from neo_mcp.paths import COMMAND_STATE_FILE, EXECUTION_EVIDENCE_FILE

    COMMAND_STATE_FILE.unlink(missing_ok=True)
    EXECUTION_EVIDENCE_FILE.unlink(missing_ok=True)
    manager = JobManager()
    handlers = ActionHandlers(
        manager, str(tmp_path), {"dedup-thread": str(tmp_path)}
    )
    original = handlers.handle_command
    handlers.handle_command = AsyncMock(wraps=original)
    client = MagicMock()
    client.send_response = AsyncMock()
    poller = BackendPoller(
        "dedup-deployment", client, handlers, handlers._thread_workspaces
    )
    command = {
        "request_id": "same-request",
        "thread_id": "dedup-thread",
        "action": "write_code",
        "filename": "dedup-marker.txt",
        "code": "sensitive-payload",
    }
    await poller._process_command(command)
    await poller._process_command({**command, "response_queue_name": "new-route"})
    assert handlers.handle_command.await_count == 1
    assert client.send_response.await_args.args[1]["response_queue_name"] == "new-route"
    assert (tmp_path / "dedup-marker.txt").read_text() == "sensitive-payload"
    evidence = read_evidence("dedup-thread")
    assert len(evidence) == 1
    assert evidence[0]["provenance"] == "local_daemon_observed"
    assert evidence[0]["file"]["sha256"]
    assert evidence[0]["file_before_sha256"] is None
    assert evidence[0]["file_after_sha256"] == evidence[0]["file"]["sha256"]
    assert "sensitive-payload" not in json.dumps(evidence)

    failing_client = MagicMock()
    failing_client.send_response = AsyncMock(side_effect=OSError("offline"))
    failing = BackendPoller(
        "dedup-deployment", failing_client, handlers, handlers._thread_workspaces
    )
    command2 = {**command, "request_id": "pending-request", "filename": "pending.txt"}
    with patch("neo_mcp.backend_poller.asyncio.sleep", new=AsyncMock()):
        await failing._process_command(command2)
    assert len(pending_responses("dedup-deployment")) == 1
    chained = read_evidence("dedup-thread")
    assert chained[-1]["previous_hash"] == chained[-2]["record_hash"]

    replay_client = MagicMock()
    replay_client.send_response = AsyncMock()
    replay = BackendPoller(
        "dedup-deployment", replay_client, handlers, handlers._thread_workspaces
    )
    await replay._replay_pending_responses()
    replay_client.send_response.assert_awaited_once()
    assert pending_responses("dedup-deployment") == []


@async_test
async def test_local_verification_is_separate_from_reported_completion(tmp_path: Path) -> None:
    from neo_mcp.paths import EXECUTION_EVIDENCE_FILE, VERIFICATION_RESULTS_FILE
    from neo_mcp.server import _task_status, _verify_task

    EXECUTION_EVIDENCE_FILE.unlink(missing_ok=True)
    VERIFICATION_RESULTS_FILE.unlink(missing_ok=True)
    (tmp_path / "artifact.txt").write_text("ok\n")
    handlers = ActionHandlers(
        JobManager(), str(tmp_path), {"verify-thread": str(tmp_path)}
    )
    poller = BackendPoller("verify-dep", MagicMock(), handlers, handlers._thread_workspaces)
    client = MagicMock()
    client.get_thread_status = AsyncMock(return_value={
        "status": "COMPLETED",
        "executor_activity": "I verified everything",
    })
    before = await _task_status(client, {"thread_id": "verify-thread"})
    assert before["status"] == "COMPLETED"
    assert before["verification_status"] == "NOT_RUN"

    verified = await _verify_task(poller, {
        "thread_id": "verify-thread",
        "files": [{"path": "artifact.txt", "exact_text": "ok\n"}],
        "allowed_files": ["artifact.txt"],
        "commands": ["test \"$(cat artifact.txt)\" = ok"],
    })
    assert verified["verification_status"] == "VERIFIED"
    after = await _task_status(client, {"thread_id": "verify-thread"})
    assert after["verification_status"] == "VERIFIED"

    failed = await _verify_task(poller, {
        "thread_id": "verify-thread",
        "files": [{"path": "artifact.txt", "exact_text": "wrong"}],
    })
    assert failed["verification_status"] == "FAILED"


def test_posix_remapping_is_literal_and_quote_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    handlers = _handlers(workspace)

    windows_like = Path(r"C:\Users\Name")
    rewritten = handlers._strip_wrapper_prefixes(
        "cd /app/project/src", ["wrapper_1"], workspace=windows_like
    )
    assert str(windows_like) in rewritten
    assert handlers._remap_command_paths(r"echo /app\foo", workspace) == r"echo /app\foo"
    quoted = handlers._remap_command_paths('cat "/app/project/file.txt"', workspace)
    assert f'"{workspace}/file.txt"' in quoted
    try:
        handlers._remap_command_paths("cat /app/project/file.txt", workspace)
        raise AssertionError("expected unsafe unquoted remap rejection")
    except ValueError as exc:
        assert "quote" in str(exc)
    assert handlers._remap_to_workspace(
        Path("/app/project/nested/file.txt"), workspace
    ) == workspace / "nested" / "file.txt"
