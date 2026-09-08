"""Daemon identity handshake — VS Code /health analogue on disk."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from neo_mcp import service
from neo_mcp.action_handlers import ActionHandlers
from neo_mcp.backend_poller import BackendPoller
from neo_mcp.job_manager import JobManager
from neo_mcp.paths import PID_FILE, deployment_ready_file


DEP = "12345678-aaaa-bbbb-cccc-ddddeeeeffff"
CMDLINE = f"{sys.executable} -m neo_mcp daemon --deployment-id {DEP} /ws"


def _reset_identity_files() -> None:
    for path in (
        PID_FILE,
        service.LOCK_FILE,
        service._deployment_pid_file(DEP),
        deployment_ready_file(DEP),
    ):
        path.unlink(missing_ok=True)


def _write_json_pid(path: Path, pid: int, **extra) -> None:
    payload = {"pid": pid, **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def _identity_ok_patches(pid: int, cmdline: str = CMDLINE):
    return (
        patch.object(service.os, "getpid", return_value=pid + 1),
        patch.object(service, "_pid_alive", return_value=True),
        patch.object(service, "_pid_cmdline", return_value=cmdline),
    )


def test_neo_mcp_argv_uses_sys_executable() -> None:
    argv = service._neo_mcp_argv(DEP, "/ws")
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "neo_mcp", "daemon"]
    assert "--deployment-id" in argv
    assert DEP in argv
    assert argv[-1] == "/ws"
    assert not argv[0].endswith("neo-mcp")


def test_pid_file_stem_strips_hyphens() -> None:
    from neo_mcp.setup import _daemon_pid_file
    p1 = service._deployment_pid_file("contract-test")
    assert p1.name == "daemon_contract.pid"
    p2 = _daemon_pid_file("contract-test")
    assert os.path.basename(p2) == "daemon_contract.pid"
    p3 = service._deployment_pid_file(DEP)
    assert p3.name == "daemon_12345678.pid"


def test_read_identity_integer_has_no_version(tmp_path: Path) -> None:
    path = tmp_path / "daemon.pid"
    path.write_text("4242\n")
    ident = service.read_identity(path)
    assert ident == {"pid": 4242}
    assert "version" not in ident


def test_integer_pid_file_is_foreign_not_stale() -> None:
    _reset_identity_files()
    pid = 4242
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))
    patches = _identity_ok_patches(pid)
    with patches[0], patches[1], patches[2]:
        probe = service.probe_daemon(DEP)
    assert probe["identity_ok"] is False
    assert probe["liveness_ok"] is True
    assert "foreign" in probe["detail"] or "legacy" in probe["detail"]
    assert "pre-handshake" not in probe["detail"]
    assert probe["heartbeat_ok"] is False
    assert probe["ok"] is False


def test_matching_json_identity_without_ready_is_not_current() -> None:
    _reset_identity_files()
    pid = 4242
    data = service.identity_payload(pid, DEP)
    data["pid"] = pid
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data) + "\n")
    patches = _identity_ok_patches(pid)
    with patches[0], patches[1], patches[2]:
        probe = service.probe_daemon(DEP)
        assert probe["identity_ok"] is True
        assert probe["heartbeat_ok"] is False
        assert service.is_current_daemon(DEP) is False


def test_npm_bare_int_with_fresh_ready_is_capable() -> None:
    _reset_identity_files()
    pid = 48870
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))
    deployment_ready_file(DEP).write_text(json.dumps({
        "deployment_id": DEP,
        "pid": pid,
        "impl": "npm",
        "version": "1.1.30",
        "observed_at": time.time(),
        "submission_intents": [],
    }))
    cmdline = f"node /usr/local/bin/neo-mcp-daemon --mcp {DEP}"
    patches = _identity_ok_patches(pid, cmdline)
    with patches[0], patches[1], patches[2]:
        probe = service.probe_daemon(DEP)
        assert probe["identity_ok"] is False
        assert probe["heartbeat_ok"] is True
        assert probe["ok"] is True
        assert service.is_current_daemon(DEP) is True
        with patch.object(service, "stop_daemon") as stop:
            assert service.ensure_current_daemon("sk-v1-test", DEP, "/ws") is True
            stop.assert_not_called()


def test_version_mismatch_is_stale() -> None:
    _reset_identity_files()
    pid = 4242
    _write_json_pid(
        PID_FILE,
        pid,
        version="0.0.1",
        executable=sys.executable,
    )
    patches = _identity_ok_patches(pid)
    with patches[0], patches[1], patches[2]:
        probe = service.probe_daemon(DEP)
    assert probe["identity_ok"] is False
    assert "0.0.1" in probe["detail"]


def test_executable_mismatch_is_stale_even_when_version_matches() -> None:
    _reset_identity_files()
    pid = 4242
    _write_json_pid(
        PID_FILE,
        pid,
        version=service.package_version(),
        executable="/opt/anaconda3/bin/python3.13",
    )
    patches = _identity_ok_patches(pid)
    with patches[0], patches[1], patches[2]:
        probe = service.probe_daemon(DEP)
    assert probe["identity_ok"] is False
    assert "foreign" in probe["detail"]


def test_fresh_ready_file_makes_probe_ok() -> None:
    _reset_identity_files()
    pid = 4242
    data = service.identity_payload(pid, DEP)
    data["pid"] = pid
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data) + "\n")
    ready = deployment_ready_file(DEP)
    ready.write_text(json.dumps({
        "deployment_id": DEP,
        "impl": "python",
        "version": service.package_version(),
        "observed_at": time.time(),
        "submission_intents": [],
    }))
    patches = _identity_ok_patches(pid)
    with patches[0], patches[1], patches[2]:
        probe = service.probe_daemon(DEP)
    assert probe["identity_ok"] is True
    assert probe["heartbeat_ok"] is True
    assert probe["ok"] is True


def test_stale_heartbeat_fails_doctor_ok_bit() -> None:
    _reset_identity_files()
    pid = 4242
    data = service.identity_payload(pid, DEP)
    data["pid"] = pid
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data) + "\n")
    ready = deployment_ready_file(DEP)
    ready.write_text(json.dumps({
        "deployment_id": DEP,
        "version": service.package_version(),
        "observed_at": time.time() - 60,
        "submission_intents": [],
    }))
    patches = _identity_ok_patches(pid)
    with patches[0], patches[1], patches[2]:
        probe = service.probe_daemon(DEP)
    assert probe["identity_ok"] is True
    assert probe["heartbeat_ok"] is False
    assert probe["ok"] is False


def test_ensure_current_stops_then_spawns_when_incapable() -> None:
    events: list[str] = []

    def fake_stop(deployment_id: str, timeout: float = 8.0) -> int:
        events.append("stop")
        return 1

    def fake_spawn(*args, **kwargs) -> bool:
        events.append("spawn")
        return True

    with (
        patch.object(service, "probe_daemon", return_value={"heartbeat_ok": False}),
        patch.object(service, "stop_daemon", side_effect=fake_stop),
        patch.object(service, "spawn_detached_daemon", side_effect=fake_spawn),
    ):
        assert service.ensure_current_daemon("sk-v1-test", DEP, "/ws") is True
    assert events == ["stop", "spawn"]


def test_ensure_current_does_not_stop_when_secret_missing() -> None:
    with (
        patch.object(service, "probe_daemon", return_value={"heartbeat_ok": False, "pid": 1}),
        patch.object(service, "stop_daemon") as stop,
        patch.object(service, "spawn_detached_daemon") as spawn,
    ):
        assert service.ensure_current_daemon("", DEP, "/ws") is False
    stop.assert_not_called()
    spawn.assert_not_called()


def test_running_daemon_pids_reads_json_pid_file() -> None:
    _reset_identity_files()
    pid = 4242
    _write_json_pid(PID_FILE, pid, version="0.5.14", executable=sys.executable)
    with (
        patch.object(service, "_pid_alive", return_value=True),
        patch.object(service, "_pid_cmdline", return_value=CMDLINE),
        patch.object(service.os, "getpid", return_value=1),
    ):
        assert pid in service.running_daemon_pids(DEP)


def test_doctor_fails_on_alive_but_stale() -> None:
    import neo_mcp.server as srv

    _reset_identity_files()
    pid = 4242
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))
    os.environ["NEO_SECRET_KEY"] = "sk-v1-test"
    os.environ["NEO_DEPLOYMENT_ID"] = DEP
    try:
        patches = _identity_ok_patches(pid)
        with patches[0], patches[1], patches[2]:
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = srv._cmd_doctor(json_mode=True)
        assert rc == 1
        payload = json.loads(out.getvalue())
        liveness = next(c for c in payload["checks"] if c["name"] == "daemon_liveness")
        identity = next(c for c in payload["checks"] if c["name"] == "daemon_identity")
        capability = next(c for c in payload["checks"] if c["name"] == "daemon_capability")
        assert liveness["ok"] is True
        assert identity["ok"] is False
        assert capability["ok"] is False
        assert payload["all_ok"] is False
        assert "foreign" in identity["detail"] or "legacy" in identity["detail"]
        assert "pre-handshake" not in identity["detail"]
        assert any("neo-mcp.log" in h for h in payload["hints"])
    finally:
        os.environ.pop("NEO_SECRET_KEY", None)
        os.environ.pop("NEO_DEPLOYMENT_ID", None)


def test_write_readiness_does_not_need_poll(tmp_path: Path) -> None:
    from neo_mcp.ipc_state import read_json_object

    deployment_id = "park-ready-deployment"
    intent = "__submission__:10000000-0000-4000-8000-000000000099"
    handlers = ActionHandlers(JobManager(), str(tmp_path), {})
    client = MagicMock()
    client.poll_deployment = AsyncMock(return_value=[])
    poller = BackendPoller(deployment_id, client, handlers, {})
    poller._thread_statuses = {intent: "RUNNING"}
    poller._write_readiness()
    client.poll_deployment.assert_not_called()
    readiness = read_json_object(deployment_ready_file(deployment_id))
    assert intent in readiness["submission_intents"]
    assert readiness["deployment_id"] == deployment_id
    assert readiness["impl"] == "python"
    assert "version" in readiness
    assert "observed_at" in readiness


def test_parked_loop_heartbeats_without_poll(tmp_path: Path) -> None:
    asyncio.run(_parked_heartbeat(tmp_path))


async def _parked_heartbeat(tmp_path: Path) -> None:
    from neo_mcp.ipc_state import read_json_object

    deployment_id = "park-heartbeat-deployment"
    handlers = ActionHandlers(JobManager(), str(tmp_path), {})
    client = MagicMock()
    client.poll_deployment = AsyncMock(return_value=[])
    poller = BackendPoller(deployment_id, client, handlers, {})
    poller._park_tick = 0.02
    task = asyncio.create_task(poller.run())
    try:
        await asyncio.sleep(0.12)
    finally:
        poller.stop()
        await asyncio.wait_for(task, timeout=1.0)
    client.poll_deployment.assert_not_called()
    readiness = read_json_object(deployment_ready_file(deployment_id))
    assert readiness["deployment_id"] == deployment_id
    assert readiness["submission_intents"] == []
    assert isinstance(readiness.get("observed_at"), (int, float))


def test_sandbox_not_ready_is_not_retryable(tmp_path: Path) -> None:
    asyncio.run(_sandbox_not_ready(tmp_path))


async def _sandbox_not_ready(tmp_path: Path) -> None:
    from neo_mcp.paths import WORKSPACE_LEASES_FILE
    from neo_mcp.server import _submit_task
    from neo_mcp.workspace_leases import release_workspace

    deployment_id = "not-ready-deployment"
    submission_id = "10000000-0000-4000-8000-000000000011"
    WORKSPACE_LEASES_FILE.unlink(missing_ok=True)
    deployment_ready_file(deployment_id).unlink(missing_ok=True)
    client = MagicMock()
    client.init_chat = AsyncMock(return_value={"thread_id": "never"})
    poller = MagicMock()
    byok = MagicMock()
    byok.resolve_active_headers.return_value = ({}, None)
    os.environ["NEO_POLLER_READY_TIMEOUT_SECONDS"] = "0.15"
    try:
        with (
            patch("neo_mcp.server.build_context_prefix", new=AsyncMock(return_value="")),
            patch(
                "neo_mcp.server.IntegrationManager.format_integrations_xml",
                return_value="",
            ),
            patch("neo_mcp.server.ensure_current_daemon", return_value=True),
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
    finally:
        os.environ.pop("NEO_POLLER_READY_TIMEOUT_SECONDS", None)
        release_workspace(workspace=str(tmp_path), submission_id=submission_id)
    assert result["code"] == "SANDBOX_NOT_READY"
    assert result["retryable"] is False
    assert result.get("permanent") is True
    assert "submission_id" not in result
    assert "poller-ready" in result["detail"]
    client.init_chat.assert_not_called()


def test_watcher_heartbeats_during_long_handler(tmp_path: Path) -> None:
    asyncio.run(_watcher_heartbeats(tmp_path))


async def _watcher_heartbeats(tmp_path: Path) -> None:
    deployment_id = "long-cmd-heartbeat"
    handlers = ActionHandlers(JobManager(), str(tmp_path), {})
    client = MagicMock()
    client.poll_deployment = AsyncMock(return_value=[])
    poller = BackendPoller(deployment_id, client, handlers, {})
    poller._park_tick = 0.08
    poller._running = True
    poller._write_readiness()
    watcher = asyncio.create_task(poller._watch_status_file())
    ages: list[float] = []
    try:
        deadline = time.time() + 0.7
        while time.time() < deadline:
            await asyncio.sleep(0.12)
            ready = json.loads(deployment_ready_file(deployment_id).read_text())
            ages.append(time.time() - float(ready["observed_at"]))
    finally:
        poller._running = False
        await asyncio.wait_for(watcher, timeout=1.0)
    assert ages
    assert max(ages) < 1.0


def test_doctor_passes_foreign_capable_daemon() -> None:
    import neo_mcp.server as srv

    _reset_identity_files()
    pid = 48870
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))
    deployment_ready_file(DEP).write_text(json.dumps({
        "deployment_id": DEP,
        "pid": pid,
        "impl": "npm",
        "version": "1.1.30",
        "observed_at": time.time(),
        "submission_intents": [],
    }))
    os.environ["NEO_SECRET_KEY"] = "sk-v1-test"
    os.environ["NEO_DEPLOYMENT_ID"] = DEP
    cmdline = "node /usr/local/bin/neo-mcp-daemon"
    try:
        patches = _identity_ok_patches(pid, cmdline)
        with patches[0], patches[1], patches[2]:
            out = io.StringIO()
            with patch("sys.stdout", out):
                rc = srv._cmd_doctor(json_mode=True)
        payload = json.loads(out.getvalue())
        identity = next(c for c in payload["checks"] if c["name"] == "daemon_identity")
        capability = next(c for c in payload["checks"] if c["name"] == "daemon_capability")
        liveness = next(c for c in payload["checks"] if c["name"] == "daemon_liveness")
        assert liveness["ok"] is True
        assert capability["ok"] is True
        assert identity["ok"] is True
        assert rc == 0
        assert payload["all_ok"] is True
    finally:
        os.environ.pop("NEO_SECRET_KEY", None)
        os.environ.pop("NEO_DEPLOYMENT_ID", None)
        _reset_identity_files()
