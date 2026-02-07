from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class JobMode(StrEnum):
    YOLO = "YOLO"
    SAFE = "SAFE"


class JobStatus(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    LOST = "LOST"


TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.TIMEOUT,
    JobStatus.LOST,
}


class JobCreate(BaseModel):
    engine: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    mode: JobMode
    prompt: str = Field(min_length=1)
    timeout_seconds: float | None = Field(default=None, gt=0, le=24 * 60 * 60)


class JobInput(BaseModel):
    text: str = Field(min_length=1)


class Job(BaseModel):
    id: int
    engine: str
    repo: str
    branch: str
    mode: JobMode
    status: JobStatus
    prompt: str
    stdout_log_path: str
    stderr_log_path: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    commit_hash: str | None = None
    timeout_seconds: float | None = None
    input_prompt: str | None = None
    artifacts: list[str] = Field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
