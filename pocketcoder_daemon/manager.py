from __future__ import annotations

from pathlib import Path
import subprocess

from pocketcoder_daemon.adapters.mock import InteractiveAdapter, MockAdapter, SleepyAdapter
from pocketcoder_daemon.db import JobStore
from pocketcoder_daemon.models import Job, JobCreate, JobMode, JobStatus, utcnow
from pocketcoder_daemon.runner import ExecutionRunner


class ConflictError(Exception):
    pass


class UnknownEngineError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        self.store = JobStore(root / "data")
        self.runner = ExecutionRunner(self.store, root / "logs")
        self.adapters = {
            "mock": MockAdapter(),
            "sleepy": SleepyAdapter(),
            "interactive": InteractiveAdapter(),
        }
        self.store.mark_active_lost()

    async def create_job(self, payload: JobCreate) -> Job:
        if self.store.has_active_for_repo(payload.repo):
            raise ConflictError("Active job already exists for repository")
        adapter = self.adapters.get(payload.engine)
        if adapter is None:
            raise UnknownEngineError(f"Unknown engine: {payload.engine}")

        capabilities = adapter.detect_capabilities()
        if payload.mode == JobMode.YOLO and not capabilities.supports_yolo:
            raise InvalidStateError(f"Engine {payload.engine} does not support YOLO mode")
        if payload.mode == JobMode.YOLO and not capabilities.supports_noninteractive:
            raise InvalidStateError(
                f"Engine {payload.engine} cannot run non-interactively in YOLO mode"
            )

        branch = f"pc/{payload.repo}/{utcnow().strftime('%Y%m%d%H%M%S')}"
        stdout_path = self.root / "logs" / f"job-{{id}}.stdout.log"
        stderr_path = self.root / "logs" / f"job-{{id}}.stderr.log"
        job = self.store.create(
            {
                "engine": payload.engine,
                "repo": payload.repo,
                "branch": branch,
                "mode": payload.mode,
                "status": JobStatus.STARTING,
                "prompt": payload.prompt,
                "stdout_log_path": str(stdout_path).format(id="placeholder"),
                "stderr_log_path": str(stderr_path).format(id="placeholder"),
                "created_at": utcnow().isoformat(),
                "timeout_seconds": payload.timeout_seconds,
            }
        )
        workspace = self.root / "projects" / payload.repo
        workspace.mkdir(parents=True, exist_ok=True)
        self._prepare_branch(workspace, branch)
        self.store.update(
            job.id,
            stdout_log_path=str(stdout_path).format(id=job.id),
            stderr_log_path=str(stderr_path).format(id=job.id),
        )
        await self.runner.run(
            job.id,
            adapter,
            payload.prompt,
            payload.mode,
            branch=branch,
            timeout_seconds=payload.timeout_seconds,
            cwd=workspace,
        )
        return self.store.get(job.id)

    async def cancel(self, job_id: int) -> Job:
        self.store.get(job_id)
        await self.runner.cancel(job_id)
        return self.store.get(job_id)

    async def send_input(self, job_id: int, text: str) -> Job:
        self.store.get(job_id)
        try:
            await self.runner.send_input(job_id, text)
        except RuntimeError as exc:
            raise InvalidStateError(str(exc)) from exc
        return self.store.get(job_id)

    async def shutdown(self) -> None:
        await self.runner.shutdown()

    def get_job(self, job_id: int) -> Job:
        return self.store.get(job_id)

    def list_jobs(self) -> list[Job]:
        return self.store.list()

    def _prepare_branch(self, workspace: Path, branch: str) -> None:
        if not (workspace / ".git").exists():
            return
        result = subprocess.run(
            ["git", "checkout", "-B", branch],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise InvalidStateError(
                result.stderr.strip() or result.stdout.strip() or "Failed to prepare git branch"
            )
