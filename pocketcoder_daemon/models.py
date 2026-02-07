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
    input_options: list[str] = Field(default_factory=list)
    stdout_preview: str | None = None
    artifacts: list[str] = Field(default_factory=list)


class EngineCapabilitiesView(BaseModel):
    supports_json_events: bool
    supports_noninteractive: bool
    supports_yolo: bool
    requires_pty: bool


class EngineInfo(BaseModel):
    name: str
    capabilities: EngineCapabilitiesView


class HealthStatus(BaseModel):
    status: str
    started_at: datetime
    now: datetime
    uptime_seconds: float


class ReadinessStatus(BaseModel):
    status: str
    db_ok: bool
    disk_ok: bool
    min_free_disk_mb: float
    free_disk_mb: float


class CLIToolStatus(BaseModel):
    name: str
    binary: str
    resolved_path: str | None = None
    available: bool
    version: str | None = None
    install_command_configured: bool
    update_command_configured: bool
    last_action: str | None = None
    last_action_status: str | None = None
    last_action_message: str | None = None
    last_action_at: datetime | None = None


class CLIActionResult(BaseModel):
    name: str
    action: str
    status: str
    message: str
    at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
