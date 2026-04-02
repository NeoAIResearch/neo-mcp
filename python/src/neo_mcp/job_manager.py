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

MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB per stream
JOB_TTL = 24 * 60 * 60            # 24 hours in seconds


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
        self, cmd: str, working_directory: str, thread_id: str
    ) -> str:
        """Start a subprocess and return its job_id immediately."""
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
        task = asyncio.create_task(self._run(job, cmd, working_directory))
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

        if job.pid is not None:
            try:
                os.kill(job.pid, signal.SIGTERM)
                logger.info("Sent SIGTERM to job %s pid %s", job_id, job.pid)
                asyncio.get_event_loop().call_later(
                    5.0, self._force_kill, job
                )
            except ProcessLookupError:
                pass  # process already gone

        if job._task and not job._task.done():
            job._task.cancel()

        job.completed_at = datetime.now(timezone.utc)
        job.exit_code = -1
        return True

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

    async def _run(self, job: _Job, cmd: str, cwd: str) -> None:
        """Background asyncio task: run cmd, stream output to job buffers."""
        stdout_path = JOBS_LOG_DIR / f"{job.job_id}.stdout.log"
        stderr_path = JOBS_LOG_DIR / f"{job.job_id}.stderr.log"

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            job.pid = proc.pid

            # Stream stdout and stderr concurrently into memory + log files
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    self._stream_output(proc.stdout, job, "stdout", stdout_path)
                )
                tg.create_task(
                    self._stream_output(proc.stderr, job, "stderr", stderr_path)
                )

            job.exit_code = await proc.wait()
        except asyncio.CancelledError:
            job.exit_code = -1
        except Exception as exc:  # noqa: BLE001
            logger.error("Job %s crashed: %s", job.job_id, exc)
            job.stderr += f"\n[Error: {exc}]"
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
        try:
            with open(log_path, "ab") as fh:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    fh.write(chunk)
                    fh.flush()
                    # Append to in-memory buffer with size cap
                    buf = getattr(job, name) + text
                    if len(buf) > MAX_LOG_BYTES:
                        keep = int(MAX_LOG_BYTES * 0.8)
                        buf = buf[-keep:]
                    setattr(job, name, buf)
        except Exception as exc:  # noqa: BLE001
            logger.debug("_stream_output %s error: %s", name, exc)

    def _stop_log_tailing(self, job: _Job) -> None:
        if job._task and not job._task.done():
            job._task.cancel()

    def _force_kill(self, job: _Job) -> None:
        if job.completed_at is not None and job.exit_code != -1:
            return  # exited cleanly already
        if job.pid is not None:
            try:
                os.kill(job.pid, signal.SIGKILL)
                logger.warning("Sent SIGKILL to job %s pid %s", job.job_id, job.pid)
            except ProcessLookupError:
                pass
        job.completed_at = datetime.now(timezone.utc)
        job.exit_code = -1
