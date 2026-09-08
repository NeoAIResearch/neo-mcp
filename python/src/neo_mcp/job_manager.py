"""Async subprocess lifecycle manager.

Mirrors DaemonJobManager.ts:
  - create_job        — spawn sh -c <cmd>, returns job_id immediately
  - get_job_logs      — snapshot of stdout/stderr/exit_code
  - terminate_job     — SIGTERM → SIGKILL after 5 s
  - cleanup_old_jobs  — remove jobs older than JOB_TTL

Each job runs as an asyncio background task and accumulates output in memory
(capped at MAX_LOG_BYTES).  Output is also written to per-job log files so it
survives if the in-memory buffer is truncated.
"""

import asyncio
import hashlib
import logging
import os
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .paths import JOBS_LOG_DIR

logger = logging.getLogger(__name__)

MAX_LOG_BYTES = int(os.environ.get("NEO_JOB_MAX_LOG_BYTES", str(10 * 1024 * 1024)))
JOB_TTL = 24 * 60 * 60            # 24 hours in seconds
JOB_MAX_RUNTIME = float(os.environ.get("NEO_JOB_MAX_RUNTIME_SECONDS", str(30 * 60)))


@dataclass
class _Job:
    job_id: str
    pid: Optional[int]
    command: str
    working_directory: str
    thread_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_sha256: Optional[str] = None
    stderr_sha256: Optional[str] = None
    _proc: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, repr=False)


class JobManager:
    """Thread-safe (single-event-loop) async job manager."""

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        JOBS_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_job(
        self,
        cmd: str,
        working_directory: str,
        thread_id: str,
        extra_env: Optional[dict[str, str]] = None,
    ) -> str:
        """Start a subprocess and return its job_id immediately.

        ``extra_env`` is merged on top of ``os.environ`` in the child process
        (last-write-wins) — used by ActionHandlers to inject credentials
        from configured integrations (Anthropic, HF, GitHub PAT, etc.).
        """
        job_id = str(uuid.uuid4())
        job = _Job(
            job_id=job_id,
            pid=None,
            command=cmd,
            working_directory=working_directory,
            thread_id=thread_id,
            started_at=datetime.now(timezone.utc),
        )
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run(job, cmd, working_directory, extra_env))
        job._task = task
        logger.info("Job created: job_id=%s cmd=%r cwd=%s", job_id, cmd[:80], working_directory)
        return job_id

    def get_job_logs(self, job_id: str) -> Optional[dict]:
        """Return a snapshot of the job's output, or None if not found."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job_id,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "exit_code": job.exit_code,
            "status": "completed" if job.completed_at is not None else "running",
            "started_at": job.started_at.isoformat(),
            "completed_at": (
                job.completed_at.isoformat() if job.completed_at else None
            ),
            "timed_out": job.timed_out,
            "stdout_truncated": job.stdout_truncated,
            "stderr_truncated": job.stderr_truncated,
            "stdout_sha256": job.stdout_sha256,
            "stderr_sha256": job.stderr_sha256,
        }

    def terminate_job(self, job_id: str) -> bool:
        """Send SIGTERM to the job, schedule SIGKILL after 5 s.

        Returns True if the job exists (even if already completed), False if
        the job_id is unknown.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.completed_at is not None:
            return True  # already done

        if job._proc is None:
            if job._task and not job._task.done():
                job._task.cancel()
            job.completed_at = datetime.now(timezone.utc)
            job.exit_code = -1
            return True
        self._signal_process_group(job, signal.SIGTERM)
        logger.info("Sent SIGTERM to job %s pid %s", job_id, job.pid)
        asyncio.get_event_loop().call_later(5.0, self._force_kill, job)
        return True

    def terminate_thread_jobs(self, thread_id: str) -> int:
        """Terminate all active jobs owned by ``thread_id``."""
        count = 0
        for job in list(self._jobs.values()):
            if job.thread_id == thread_id and job.completed_at is None:
                if self.terminate_job(job.job_id):
                    count += 1
        return count

    async def wait_for_job(self, job_id: str) -> Optional[dict]:
        """Wait for a managed job and return its final bounded log snapshot."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job._task is not None:
            await asyncio.shield(job._task)
        return self.get_job_logs(job_id)

    def cleanup_old_jobs(self) -> None:
        """Remove completed jobs older than JOB_TTL from memory."""
        now = datetime.now(timezone.utc).timestamp()
        stale = [
            jid
            for jid, j in self._jobs.items()
            if j.exit_code is not None and (now - j.started_at.timestamp()) > JOB_TTL
        ]
        for jid in stale:
            j = self._jobs.pop(jid)
            self._stop_log_tailing(j)
        if stale:
            logger.info("Cleaned up %d old jobs", len(stale))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run(
        self,
        job: _Job,
        cmd: str,
        cwd: str,
        extra_env: Optional[dict[str, str]] = None,
    ) -> None:
        """Background asyncio task: run cmd, stream output to job buffers."""
        stdout_path = JOBS_LOG_DIR / f"{job.job_id}.stdout.log"
        stderr_path = JOBS_LOG_DIR / f"{job.job_id}.stderr.log"

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            job._proc = proc
            job.pid = proc.pid

            # Stream stdout and stderr concurrently into memory + log files,
            # with a hard timeout to kill hung subprocesses.
            try:
                async with asyncio.timeout(JOB_MAX_RUNTIME):
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(
                            self._stream_output(proc.stdout, job, "stdout", stdout_path)
                        )
                        tg.create_task(
                            self._stream_output(proc.stderr, job, "stderr", stderr_path)
                        )
            except TimeoutError:
                job.timed_out = True
                logger.warning(
                    "Job %s exceeded max runtime (%ds) — killing", job.job_id, JOB_MAX_RUNTIME
                )
                self._append_capped(
                    job, "stderr",
                    f"\n[Killed: exceeded {JOB_MAX_RUNTIME}s max runtime]",
                )
                self._signal_process_group(job, signal.SIGKILL)

            job.exit_code = await proc.wait()
        except asyncio.CancelledError:
            # Kill the subprocess so the asyncio event loop doesn't wait for it.
            if proc is not None:
                self._signal_process_group(job, signal.SIGKILL)
                await proc.wait()
            job.exit_code = -1
        except Exception as exc:  # noqa: BLE001
            logger.error("Job %s crashed: %s", job.job_id, exc)
            self._append_capped(job, "stderr", f"\n[Error: {exc}]")
            job.exit_code = -1
        finally:
            job.completed_at = datetime.now(timezone.utc)
            logger.info(
                "Job %s finished: exit_code=%s stdout=%d stderr=%d",
                job.job_id,
                job.exit_code,
                len(job.stdout),
                len(job.stderr),
            )

    async def _stream_output(
        self,
        stream: Optional[asyncio.StreamReader],
        job: _Job,
        name: str,
        log_path: Path,
    ) -> None:
        if stream is None:
            return
        digest = hashlib.sha256()
        try:
            with open(log_path, "ab") as fh:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    digest.update(chunk)
                    fh.write(chunk)
                    fh.flush()
                    self._append_capped(job, name, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("_stream_output %s error: %s", name, exc)
        finally:
            setattr(job, f"{name}_sha256", digest.hexdigest())

    def _stop_log_tailing(self, job: _Job) -> None:
        if job._task and not job._task.done():
            job._task.cancel()

    def _force_kill(self, job: _Job) -> None:
        if job.completed_at is not None:
            return  # exited cleanly already
        self._signal_process_group(job, signal.SIGKILL)
        logger.warning("Sent SIGKILL to job %s pid %s", job.job_id, job.pid)

    @staticmethod
    def _signal_process_group(job: _Job, sig: signal.Signals) -> None:
        """Signal the job's dedicated process group, never a reused bare PID."""
        proc = job._proc
        if proc is None or proc.returncode is not None or job.pid is None:
            return
        try:
            os.killpg(job.pid, sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _append_capped(job: _Job, name: str, text: str) -> None:
        """Append text while enforcing a UTF-8 byte cap per stream."""
        current = (getattr(job, name) + text).encode("utf-8", errors="replace")
        if len(current) > MAX_LOG_BYTES:
            current = current[-int(MAX_LOG_BYTES * 0.8):]
            # Avoid beginning in the middle of a UTF-8 sequence.
            value = current.decode("utf-8", errors="ignore")
            setattr(job, f"{name}_truncated", True)
        else:
            value = current.decode("utf-8", errors="replace")
        setattr(job, name, value)
