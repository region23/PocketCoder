from __future__ import annotations

import json
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
        self.conn.execute("PRAGMA foreign_keys = ON")
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
                commit_hash TEXT,
                timeout_seconds REAL,
                input_prompt TEXT,
                input_options TEXT,
                stdout_preview TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS engines (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS repos (
                name TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                kind TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
            """
        )
        self._ensure_column("jobs", "timeout_seconds", "REAL")
        self._ensure_column("jobs", "input_prompt", "TEXT")
        self._ensure_column("jobs", "input_options", "TEXT")
        self._ensure_column("jobs", "stdout_preview", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        known = {row["name"] for row in rows}
        if column not in known:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _to_job(self, row: sqlite3.Row) -> Job:
        payload = dict(row)
        payload["input_options"] = self._decode_json_list(payload.get("input_options"))
        payload["artifacts"] = self._list_artifacts(payload["id"])
        return Job.model_validate(payload)

    @staticmethod
    def _decode_json_list(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        result: list[str] = []
        for item in parsed:
            if isinstance(item, str):
                result.append(item)
        return result

    def _remember_catalog(self, engine: str, repo: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO engines (name) VALUES (?)", (engine,))
        self.conn.execute("INSERT OR IGNORE INTO repos (name) VALUES (?)", (repo,))

    def create(self, payload: dict) -> Job:
        self._remember_catalog(payload["engine"], payload["repo"])
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
        return self._to_job(row)

    def list(self) -> list[Job]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
        return [self._to_job(row) for row in rows]

    def update(self, job_id: int, **extras: str | float | list[str] | None) -> Job:
        if not extras:
            return self.get(job_id)
        assignments: list[str] = []
        values: list[str | float | None] = []
        for key, value in extras.items():
            if key == "input_options":
                if value is None:
                    value = None
                elif isinstance(value, list):
                    value = json.dumps(value)
                else:
                    raise TypeError("input_options must be list[str] | None")
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(job_id)
        self.conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
            tuple(values),
        )
        self.conn.commit()
        return self.get(job_id)

    def update_status(
        self,
        job_id: int,
        status: JobStatus,
        **extras: str | float | list[str] | None,
    ) -> Job:
        return self.update(job_id, status=status, **extras)

    def add_artifact(self, job_id: int, path: str, kind: str = "log") -> None:
        created_at = self.conn.execute("SELECT datetime('now')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO artifacts (job_id, path, kind, created_at) VALUES (?, ?, ?, ?)",
            (job_id, path, kind, created_at),
        )
        self.conn.commit()

    def _list_artifacts(self, job_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT path FROM artifacts WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
        return [row["path"] for row in rows]

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

    def ping(self) -> bool:
        row = self.conn.execute("SELECT 1").fetchone()
        return row is not None

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
        ).fetchall()
        result: dict[str, int] = {}
        for row in rows:
            result[str(row["status"])] = int(row["cnt"])
        return result
