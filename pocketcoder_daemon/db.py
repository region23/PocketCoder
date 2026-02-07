from __future__ import annotations

import sqlite3
from pathlib import Path

from pocketcoder_daemon.models import Job, JobStatus, TERMINAL_STATUSES


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "pocketcoder.db"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine TEXT NOT NULL,
                repo TEXT NOT NULL,
                branch TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                stdout_log_path TEXT NOT NULL,
                stderr_log_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                commit_hash TEXT
            )
            """
        )
        self.conn.commit()

    def create(self, payload: dict) -> Job:
        keys = ",".join(payload.keys())
        placeholders = ",".join(["?"] * len(payload))
        cur = self.conn.execute(
            f"INSERT INTO jobs ({keys}) VALUES ({placeholders})", tuple(payload.values())
        )
        self.conn.commit()
        return self.get(cur.lastrowid)

    def get(self, job_id: int) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return Job.model_validate(dict(row))

    def list(self) -> list[Job]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
        return [Job.model_validate(dict(row)) for row in rows]

    def update_status(self, job_id: int, status: JobStatus, **extras: str | None) -> Job:
        assignments = ["status = ?"]
        values: list[str | None] = [status]
        for key, value in extras.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(job_id)
        self.conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", tuple(values)
        )
        self.conn.commit()
        return self.get(job_id)

    def has_active_for_repo(self, repo: str) -> bool:
        placeholders = ",".join(["?"] * len(TERMINAL_STATUSES))
        row = self.conn.execute(
            f"SELECT 1 FROM jobs WHERE repo = ? AND status NOT IN ({placeholders}) LIMIT 1",
            (repo, *TERMINAL_STATUSES),
        ).fetchone()
        return row is not None

    def mark_active_lost(self) -> None:
        placeholders = ",".join(["?"] * len(TERMINAL_STATUSES))
        self.conn.execute(
            f"UPDATE jobs SET status = ? WHERE status NOT IN ({placeholders})",
            (JobStatus.LOST, *TERMINAL_STATUSES),
        )
        self.conn.commit()
