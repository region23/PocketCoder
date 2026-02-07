from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import os

from fastapi import Depends, FastAPI, HTTPException, Request

from pocketcoder_daemon.manager import (
    ConflictError,
    InvalidStateError,
    JobManager,
    UnknownEngineError,
)
from pocketcoder_daemon.models import Job, JobCreate, JobInput


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
        yield
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
