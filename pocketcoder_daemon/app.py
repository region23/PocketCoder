from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import os

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from pocketcoder_daemon.manager import (
    ConflictError,
    InvalidStateError,
    JobManager,
    ServiceError,
    UnknownEngineError,
)
from pocketcoder_daemon.models import (
    CLIActionResult,
    CLIToolStatus,
    EngineInfo,
    HealthStatus,
    Job,
    JobCreate,
    JobInput,
    ReadinessStatus,
)


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip() or str(default))
    except ValueError:
        return default


def create_app(root: Path) -> FastAPI:
    manager = JobManager(root)
    required_api_key = os.getenv("POCKETCODER_API_KEY")

    async def guard_api_key(request: Request) -> None:
        if not required_api_key:
            return
        if request.headers.get("x-api-key") != required_api_key:
            raise HTTPException(status_code=401, detail="Unauthorized")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        auto_update_task: asyncio.Task | None = None
        stop_event = asyncio.Event()

        auto_install_enabled = _env_true("POCKETCODER_CLI_AUTO_INSTALL", default=False)
        auto_update_interval = _env_float(
            "POCKETCODER_CLI_AUTO_UPDATE_INTERVAL_SECONDS",
            default=0.0,
        )

        if auto_install_enabled:
            await manager.install_missing_cli_tools()

        if auto_update_interval > 0:

            async def auto_update_loop() -> None:
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=auto_update_interval,
                        )
                    except asyncio.TimeoutError:
                        await manager.update_all_cli_tools()

            auto_update_task = asyncio.create_task(auto_update_loop())

        yield
        stop_event.set()
        if auto_update_task is not None:
            auto_update_task.cancel()
            await asyncio.gather(auto_update_task, return_exceptions=True)
        await manager.shutdown()

    app = FastAPI(
        title="pocketcoder-daemon",
        lifespan=lifespan,
        dependencies=[Depends(guard_api_key)],
    )

    @app.post("/jobs", response_model=Job, status_code=201)
    async def create_job(payload: JobCreate) -> Job:
        try:
            return await manager.create_job(payload)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnknownEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except InvalidStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/jobs", response_model=list[Job])
    async def list_jobs() -> list[Job]:
        return manager.list_jobs()

    @app.get("/health", response_model=HealthStatus)
    async def health() -> HealthStatus:
        return manager.health()

    @app.get("/ready", response_model=ReadinessStatus)
    async def ready() -> ReadinessStatus:
        result = manager.readiness()
        if result.status != "ready":
            raise HTTPException(status_code=503, detail="Service is not ready")
        return result

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(manager.metrics(), media_type="text/plain; version=0.0.4")

    @app.get("/engines", response_model=list[EngineInfo])
    async def list_engines() -> list[EngineInfo]:
        return manager.list_engines()

    @app.get("/cli/tools", response_model=list[CLIToolStatus])
    async def list_cli_tools() -> list[CLIToolStatus]:
        return manager.list_cli_tools()

    @app.post("/cli/tools/{name}/install", response_model=CLIActionResult)
    async def install_cli_tool(name: str) -> CLIActionResult:
        try:
            return await manager.install_cli_tool(name)
        except ServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/cli/tools/{name}/update", response_model=CLIActionResult)
    async def update_cli_tool(name: str) -> CLIActionResult:
        try:
            return await manager.update_cli_tool(name)
        except ServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/cli/tools/install-missing", response_model=list[CLIActionResult])
    async def install_missing_cli_tools() -> list[CLIActionResult]:
        return await manager.install_missing_cli_tools()

    @app.post("/cli/tools/update-all", response_model=list[CLIActionResult])
    async def update_all_cli_tools() -> list[CLIActionResult]:
        return await manager.update_all_cli_tools()

    @app.get("/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: int) -> Job:
        try:
            return manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.post("/jobs/{job_id}/cancel", response_model=Job)
    async def cancel_job(job_id: int) -> Job:
        try:
            return await manager.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.post("/jobs/{job_id}/input", response_model=Job)
    async def send_input(job_id: int, payload: JobInput) -> Job:
        try:
            return await manager.send_input(job_id, payload.text)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except InvalidStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
