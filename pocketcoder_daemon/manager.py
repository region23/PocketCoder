from __future__ import annotations

from datetime import timezone
import os
from pathlib import Path
import shutil
import subprocess

from pocketcoder_daemon.adapters.codex import CloudCodeAdapter, CodexAdapter, OpenCodeAdapter
from pocketcoder_daemon.adapters.mock import InteractiveAdapter, MockAdapter, SleepyAdapter
from pocketcoder_daemon.cli_tools import CLIToolError, CLIToolManager
from pocketcoder_daemon.db import JobStore
from pocketcoder_daemon.models import (
    CLIActionResult,
    CLIToolStatus,
    EngineCapabilitiesView,
    EngineInfo,
    HealthStatus,
    Job,
    JobCreate,
    JobMode,
    JobStatus,
    ReadinessStatus,
    utcnow,
)
from pocketcoder_daemon.runner import ExecutionRunner


class ConflictError(Exception):
    pass


class UnknownEngineError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class ServiceError(Exception):
    pass


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.started_at = utcnow()
        self.min_free_disk_mb = self._env_float("POCKETCODER_MIN_FREE_DISK_MB", 100.0)
        self.store = JobStore(root / "data")
        self.runner = ExecutionRunner(self.store, root / "logs")
        self.cli_tools = CLIToolManager()
        self.adapters = {
            "mock": MockAdapter(),
            "sleepy": SleepyAdapter(),
            "interactive": InteractiveAdapter(),
            "codex": CodexAdapter(),
            "cloudcode": CloudCodeAdapter(),
            "opencode": OpenCodeAdapter(),
        }
        self.store.mark_active_lost()

    async def create_job(self, payload: JobCreate) -> Job:
        if not self._disk_ok():
            raise InvalidStateError(
                "Insufficient free disk space for a new job "
                f"(free={self._free_disk_mb():.1f}MB, required={self.min_free_disk_mb:.1f}MB)"
            )
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

    def list_engines(self) -> list[EngineInfo]:
        engines: list[EngineInfo] = []
        for name, adapter in sorted(self.adapters.items()):
            caps = adapter.detect_capabilities()
            engines.append(
                EngineInfo(
                    name=name,
                    capabilities=EngineCapabilitiesView(
                        supports_json_events=caps.supports_json_events,
                        supports_noninteractive=caps.supports_noninteractive,
                        supports_yolo=caps.supports_yolo,
                        requires_pty=caps.requires_pty,
                    ),
                )
            )
        return engines

    def list_cli_tools(self) -> list[CLIToolStatus]:
        return self.cli_tools.list_tools()

    async def install_cli_tool(self, name: str) -> CLIActionResult:
        try:
            return await self.cli_tools.install_tool(name)
        except CLIToolError as exc:
            raise ServiceError(str(exc)) from exc

    async def update_cli_tool(self, name: str) -> CLIActionResult:
        try:
            return await self.cli_tools.update_tool(name)
        except CLIToolError as exc:
            raise ServiceError(str(exc)) from exc

    async def install_missing_cli_tools(self) -> list[CLIActionResult]:
        return await self.cli_tools.install_missing()

    async def update_all_cli_tools(self) -> list[CLIActionResult]:
        return await self.cli_tools.update_all()

    def health(self) -> HealthStatus:
        now = utcnow()
        uptime_seconds = (now - self.started_at).total_seconds()
        return HealthStatus(
            status="ok",
            started_at=self.started_at,
            now=now,
            uptime_seconds=uptime_seconds,
        )

    def readiness(self) -> ReadinessStatus:
        db_ok = self.store.ping()
        free_disk_mb = self._free_disk_mb()
        disk_ok = free_disk_mb >= self.min_free_disk_mb
        return ReadinessStatus(
            status="ready" if (db_ok and disk_ok) else "not_ready",
            db_ok=db_ok,
            disk_ok=disk_ok,
            min_free_disk_mb=self.min_free_disk_mb,
            free_disk_mb=free_disk_mb,
        )

    def metrics(self) -> str:
        now = utcnow()
        uptime_seconds = (now - self.started_at).total_seconds()
        counts = self.store.counts_by_status()
        lines: list[str] = []
        lines.extend(
            [
                "# HELP pocketcoder_uptime_seconds Daemon uptime in seconds.",
                "# TYPE pocketcoder_uptime_seconds gauge",
                f"pocketcoder_uptime_seconds {uptime_seconds:.3f}",
                "# HELP pocketcoder_active_runs Number of in-memory active runs.",
                "# TYPE pocketcoder_active_runs gauge",
                f"pocketcoder_active_runs {len(self.runner.active_runs)}",
                "# HELP pocketcoder_jobs_total Total jobs by status.",
                "# TYPE pocketcoder_jobs_total gauge",
            ]
        )
        for status in sorted(counts):
            lines.append(f'pocketcoder_jobs_total{{status="{status}"}} {counts[status]}')
        lines.extend(
            [
                "# HELP pocketcoder_engines_total Registered engine adapters.",
                "# TYPE pocketcoder_engines_total gauge",
                f"pocketcoder_engines_total {len(self.adapters)}",
                "# HELP pocketcoder_cli_available CLI tool availability by tool.",
                "# TYPE pocketcoder_cli_available gauge",
            ]
        )
        for tool in self.list_cli_tools():
            available = 1 if tool.available else 0
            lines.append(f'pocketcoder_cli_available{{tool="{tool.name}"}} {available}')
        free_disk_mb = self._free_disk_mb()
        lines.extend(
            [
                "# HELP pocketcoder_disk_free_megabytes Free disk space under daemon root in MB.",
                "# TYPE pocketcoder_disk_free_megabytes gauge",
                f"pocketcoder_disk_free_megabytes {free_disk_mb:.3f}",
                "# HELP pocketcoder_disk_min_required_megabytes Minimum free disk required in MB.",
                "# TYPE pocketcoder_disk_min_required_megabytes gauge",
                f"pocketcoder_disk_min_required_megabytes {self.min_free_disk_mb:.3f}",
            ]
        )
        timestamp = int(now.replace(tzinfo=timezone.utc).timestamp())
        lines.append(f"pocketcoder_metrics_timestamp_seconds {timestamp}")
        return "\n".join(lines) + "\n"

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

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw.strip() or str(default))
        except ValueError:
            return default

    def _free_disk_mb(self) -> float:
        usage = shutil.disk_usage(self.root)
        return usage.free / (1024 * 1024)

    def _disk_ok(self) -> bool:
        return self._free_disk_mb() >= self.min_free_disk_mb
