import asyncio
import gzip
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from pocketcoder_daemon.app import create_app


@pytest.mark.asyncio
async def test_create_job_and_complete(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "engine": "mock",
                "repo": "demo-repo",
                "mode": "YOLO",
                "prompt": "implement feature",
            },
        )
        assert response.status_code == 201
        created = response.json()
        job_id = created["id"]
        assert created["status"] in {"STARTING", "RUNNING"}

        for _ in range(100):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.02)

        assert detail["status"] == "COMPLETED"
        assert detail["branch"].startswith("pc/demo-repo/")

    log_gz = tmp_path / "logs" / f"job-{job_id}.log.gz"
    assert log_gz.exists()
    assert "mock engine" in gzip.decompress(log_gz.read_bytes()).decode()


@pytest.mark.asyncio
async def test_only_one_active_job_per_repo(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/jobs",
            json={"engine": "sleepy", "repo": "same", "mode": "SAFE", "prompt": "first"},
        )
        assert first.status_code == 201

        second = await client.post(
            "/jobs",
            json={"engine": "mock", "repo": "same", "mode": "SAFE", "prompt": "second"},
        )
        assert second.status_code == 409

        first_id = first.json()["id"]
        for _ in range(100):
            detail = (await client.get(f"/jobs/{first_id}")).json()
            if detail["status"] in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "LOST"}:
                break
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_cancel_job(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={"engine": "sleepy", "repo": "cancel", "mode": "SAFE", "prompt": "wait"},
        )
        job_id = created.json()["id"]

        cancelled = await client.post(f"/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200

        for _ in range(100):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] == "CANCELLED":
                break
            await asyncio.sleep(0.02)

        assert detail["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_restart_marks_active_jobs_as_lost(tmp_path: Path):
    app1 = create_app(tmp_path)
    transport1 = ASGITransport(app=app1)

    async with AsyncClient(transport=transport1, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={"engine": "mock", "repo": "restart", "mode": "SAFE", "prompt": "short"},
        )
        job_id = created.json()["id"]

        for _ in range(100):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.02)

    db = sqlite3.connect(tmp_path / "data" / "pocketcoder.db")
    db.execute("UPDATE jobs SET status = 'RUNNING' WHERE id = ?", (job_id,))
    db.commit()
    db.close()

    app2 = create_app(tmp_path)
    transport2 = ASGITransport(app=app2)
    async with AsyncClient(transport=transport2, base_url="http://test") as client:
        jobs = (await client.get("/jobs")).json()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "LOST"
