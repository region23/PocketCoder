from __future__ import annotations

import asyncio
import gzip
import os
import signal
from pathlib import Path

from pocketcoder_daemon.db import JobStore
from pocketcoder_daemon.models import JobStatus, utcnow


class ExecutionRunner:
    def __init__(self, store: JobStore, logs_dir: Path):
        self.store = store
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[int, asyncio.Task] = {}
        self.processes: dict[int, asyncio.subprocess.Process] = {}

    async def run(self, job_id: int, command: list[str]) -> None:
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, command))

    async def _run(self, job_id: int, command: list[str]) -> None:
        if self.store.get(job_id).status == JobStatus.CANCELLED:
            return

        log_path = self.logs_dir / f"job-{job_id}.log"
        self.store.update_status(job_id, JobStatus.RUNNING, started_at=utcnow().isoformat())
        with log_path.open("ab") as log:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            self.processes[job_id] = process
            stdout, _ = await process.communicate()
            if stdout:
                log.write(stdout)

        status = JobStatus.COMPLETED if process.returncode == 0 else JobStatus.FAILED
        if self.store.get(job_id).status == JobStatus.CANCELLED:
            status = JobStatus.CANCELLED
        self.store.update_status(job_id, status, finished_at=utcnow().isoformat())

        gz_path = self.logs_dir / f"job-{job_id}.log.gz"
        with log_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
            dst.write(src.read())
        log_path.unlink(missing_ok=True)
        self.processes.pop(job_id, None)

    async def cancel(self, job_id: int) -> None:
        process = self.processes.get(job_id)
        self.store.update_status(job_id, JobStatus.CANCELLED, finished_at=utcnow().isoformat())
        if not process:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def shutdown(self) -> None:
        for job_id in list(self.processes):
            await self.cancel(job_id)
        if self.tasks:
            await asyncio.wait(self.tasks.values(), timeout=2)
