from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from pocketcoder_daemon.manager import ConflictError, JobManager
from pocketcoder_daemon.models import Job, JobCreate


def create_app(root: Path) -> FastAPI:
    manager = JobManager(root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(title="pocketcoder-daemon", lifespan=lifespan)

    @app.post("/jobs", response_model=Job, status_code=201)
    async def create_job(payload: JobCreate) -> Job:
        try:
            return await manager.create_job(payload)
        except ConflictError as exc:
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

    return app
