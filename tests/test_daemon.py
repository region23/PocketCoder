import asyncio
import gzip
import sqlite3
import subprocess
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

    stdout_log_gz = tmp_path / "logs" / f"job-{job_id}.stdout.log.gz"
    stderr_log_gz = tmp_path / "logs" / f"job-{job_id}.stderr.log.gz"
    assert stdout_log_gz.exists()
    assert stderr_log_gz.exists()
    assert "mock engine" in gzip.decompress(stdout_log_gz.read_bytes()).decode()

    db = sqlite3.connect(tmp_path / "data" / "pocketcoder.db")
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    }
    assert {"jobs", "engines", "repos", "artifacts"}.issubset(tables)
    artifact_rows = db.execute(
        "SELECT kind FROM artifacts WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    db.close()
    assert [row[0] for row in artifact_rows] == ["stdout_log", "stderr_log"]


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


@pytest.mark.asyncio
async def test_interactive_job_waits_input_and_completes(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "interactive",
                "repo": "wizard",
                "mode": "SAFE",
                "prompt": "need choice",
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        waiting = None
        for _ in range(100):
            waiting = (await client.get(f"/jobs/{job_id}")).json()
            if waiting["status"] == "WAITING_INPUT":
                break
            await asyncio.sleep(0.02)
        assert waiting is not None
        assert waiting["status"] == "WAITING_INPUT"
        assert waiting["input_prompt"] == "Choose an option"

        sent = await client.post(f"/jobs/{job_id}/input", json={"text": "option-1"})
        assert sent.status_code == 200

        done = None
        for _ in range(100):
            done = (await client.get(f"/jobs/{job_id}")).json()
            if done["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.02)

        assert done is not None
        assert done["status"] == "COMPLETED"

    stdout_log_gz = tmp_path / "logs" / f"job-{job_id}.stdout.log.gz"
    assert stdout_log_gz.exists()
    assert "received: option-1" in gzip.decompress(stdout_log_gz.read_bytes()).decode()


@pytest.mark.asyncio
async def test_job_times_out(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "sleepy",
                "repo": "timeout",
                "mode": "SAFE",
                "prompt": "sleep",
                "timeout_seconds": 0.1,
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        detail = None
        for _ in range(150):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] in {"TIMEOUT", "COMPLETED", "FAILED"}:
                break
            await asyncio.sleep(0.02)

        assert detail is not None
        assert detail["status"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_unknown_engine_returns_400(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "engine": "codex",
                "repo": "unknown",
                "mode": "SAFE",
                "prompt": "test",
            },
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_yolo_with_non_yolo_engine_returns_409(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "engine": "interactive",
                "repo": "mode-check",
                "mode": "YOLO",
                "prompt": "test",
            },
        )
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_input_for_non_waiting_job_returns_409(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "sleepy",
                "repo": "no-input",
                "mode": "SAFE",
                "prompt": "run",
            },
        )
        job_id = created.json()["id"]
        response = await client.post(f"/jobs/{job_id}/input", json={"text": "hello"})
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_git_branch_prepared_and_commit_hash_recorded(tmp_path: Path):
    workspace = tmp_path / "projects" / "gitrepo"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "pocketcoder@example.com"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PocketCoder"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    (workspace / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "mock",
                "repo": "gitrepo",
                "mode": "SAFE",
                "prompt": "noop",
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        detail = None
        for _ in range(100):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.02)

    assert detail is not None
    assert detail["status"] == "COMPLETED"
    assert detail["commit_hash"]

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch.startswith("pc/gitrepo/")


@pytest.mark.asyncio
async def test_api_key_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POCKETCODER_API_KEY", "secret")
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/jobs",
            json={"engine": "mock", "repo": "guard", "mode": "SAFE", "prompt": "x"},
        )
        assert denied.status_code == 401

        allowed = await client.post(
            "/jobs",
            json={"engine": "mock", "repo": "guard", "mode": "SAFE", "prompt": "x"},
            headers={"x-api-key": "secret"},
        )
        assert allowed.status_code == 201
