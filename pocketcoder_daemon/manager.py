from __future__ import annotations

from pathlib import Path

from pocketcoder_daemon.adapters.mock import MockAdapter, SleepyAdapter
from pocketcoder_daemon.db import JobStore
from pocketcoder_daemon.models import Job, JobCreate, JobStatus, utcnow
from pocketcoder_daemon.runner import ExecutionRunner


class ConflictError(Exception):
    pass


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        self.store = JobStore(root / "data")
        self.runner = ExecutionRunner(self.store, root / "logs")
        self.adapters = {"mock": MockAdapter(), "sleepy": SleepyAdapter()}
        self.store.mark_active_lost()

    async def create_job(self, payload: JobCreate) -> Job:
        if self.store.has_active_for_repo(payload.repo):
            raise ConflictError("Active job already exists for repository")
        branch = f"pc/{payload.repo}/{utcnow().strftime('%Y%m%d%H%M%S')}"
        job = self.store.create(
            {
                "engine": payload.engine,
                "repo": payload.repo,
                "branch": branch,
                "mode": payload.mode,
                "status": JobStatus.STARTING,
                "prompt": payload.prompt,
                "stdout_log_path": str(self.root / "logs" / "placeholder"),
                "stderr_log_path": str(self.root / "logs" / "placeholder"),
                "created_at": utcnow().isoformat(),
            }
        )
        base = self.root / "logs" / f"job-{job.id}.log"
        self.store.update_status(
            job.id,
            JobStatus.STARTING,
            stdout_log_path=str(base),
            stderr_log_path=str(base),
        )
        command = self.adapters[payload.engine].build_command(payload.prompt, payload.mode)
        await self.runner.run(job.id, command)
        return self.store.get(job.id)

    async def cancel(self, job_id: int) -> Job:
        await self.runner.cancel(job_id)
        return self.store.get(job_id)

    async def shutdown(self) -> None:
        await self.runner.shutdown()

    def get_job(self, job_id: int) -> Job:
        return self.store.get(job_id)

    def list_jobs(self) -> list[Job]:
        return self.store.list()
