import asyncio
import gzip
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from pocketcoder_daemon.app import create_app


def _command_json(script: str) -> str:
    return json.dumps(["python3", "-c", script, "{prompt}"])


@pytest.fixture(autouse=True)
def configure_default_engine_commands(monkeypatch: pytest.MonkeyPatch):
    default_script = "import sys; print('engine prompt=' + sys.argv[-1])"
    for prefix in ("CODEX", "CLOUDCODE", "OPENCODE"):
        monkeypatch.setenv(
            f"POCKETCODER_{prefix}_SAFE_COMMAND_JSON",
            _command_json(default_script),
        )
        monkeypatch.setenv(
            f"POCKETCODER_{prefix}_YOLO_COMMAND_JSON",
            _command_json(default_script),
        )


@pytest.mark.asyncio
async def test_create_job_and_complete(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "engine": "codex",
                "repo": "demo-repo",
                "mode": "YOLO",
                "prompt": "implement feature",
            },
        )
        assert response.status_code == 201
        created = response.json()
        job_id = created["id"]
        assert created["status"] in {"STARTING", "RUNNING"}
        assert created["timeout_seconds"] == 24 * 60 * 60

        for _ in range(100):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.02)

        assert detail["status"] == "COMPLETED"
        assert detail["branch"].startswith("pc/demo-repo/")
        assert detail["stdout_preview"] is not None

    stdout_log_gz = tmp_path / "logs" / f"job-{job_id}.stdout.log.gz"
    stderr_log_gz = tmp_path / "logs" / f"job-{job_id}.stderr.log.gz"
    assert stdout_log_gz.exists()
    assert stderr_log_gz.exists()
    assert "engine prompt=implement feature" in gzip.decompress(stdout_log_gz.read_bytes()).decode()
    assert (tmp_path / "projects" / "demo-repo" / ".git").exists()

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
async def test_only_one_active_job_per_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "POCKETCODER_CODEX_SAFE_COMMAND_JSON",
        _command_json(
            "import time; print('started active lock'); time.sleep(0.6); print('finished active lock')"
        ),
    )
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/jobs",
            json={"engine": "codex", "repo": "same", "mode": "SAFE", "prompt": "first"},
        )
        assert first.status_code == 201

        second = await client.post(
            "/jobs",
            json={"engine": "codex", "repo": "same", "mode": "SAFE", "prompt": "second"},
        )
        assert second.status_code == 409

        first_id = first.json()["id"]
        for _ in range(100):
            detail = (await client.get(f"/jobs/{first_id}")).json()
            if detail["status"] in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "LOST"}:
                break
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_cancel_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "POCKETCODER_CODEX_SAFE_COMMAND_JSON",
        _command_json(
            "import time; print('started cancel'); time.sleep(2); print('finished cancel')"
        ),
    )
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={"engine": "codex", "repo": "cancel", "mode": "SAFE", "prompt": "wait"},
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
            json={"engine": "codex", "repo": "restart", "mode": "SAFE", "prompt": "short"},
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
async def test_interactive_job_waits_input_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "POCKETCODER_CODEX_SAFE_COMMAND_JSON",
        _command_json(
            (
                "import sys; "
                "print(f'tty stdin={sys.stdin.isatty()} stdout={sys.stdout.isatty()}', flush=True); "
                "print('INPUT_REQUIRED_JSON:{\"prompt\":\"Choose an option\",\"options\":[\"option-1\",\"option-2\"]}', flush=True); "
                "answer = sys.stdin.readline().strip(); "
                "print(f'received: {answer}', flush=True)"
            )
        ),
    )
    monkeypatch.setenv("POCKETCODER_CODEX_REQUIRES_PTY", "1")
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "codex",
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
        assert waiting["input_options"] == ["option-1", "option-2"]

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
        assert done["input_options"] == []

    stdout_log_gz = tmp_path / "logs" / f"job-{job_id}.stdout.log.gz"
    assert stdout_log_gz.exists()
    output = gzip.decompress(stdout_log_gz.read_bytes()).decode()
    assert "tty stdin=True stdout=True" in output
    assert "received: option-1" in output


@pytest.mark.asyncio
async def test_job_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "POCKETCODER_CODEX_SAFE_COMMAND_JSON",
        _command_json("import time; print('started timeout'); time.sleep(2); print('done timeout')"),
    )
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "codex",
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
                "engine": "unknown-engine",
                "repo": "unknown",
                "mode": "SAFE",
                "prompt": "test",
            },
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_job_stores_requester_metadata(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "codex",
                "repo": "meta",
                "mode": "SAFE",
                "prompt": "run",
                "requester_user_id": 101,
                "requester_chat_id": 202,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["requester_user_id"] == 101
        assert payload["requester_chat_id"] == 202

        detail = await client.get(f"/jobs/{payload['id']}")
        assert detail.status_code == 200
        reloaded = detail.json()
        assert reloaded["requester_user_id"] == 101
        assert reloaded["requester_chat_id"] == 202


@pytest.mark.asyncio
async def test_yolo_with_non_yolo_engine_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("POCKETCODER_OPENCODE_SUPPORTS_YOLO", "0")
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "engine": "opencode",
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
                "engine": "codex",
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
                "engine": "codex",
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
            json={"engine": "codex", "repo": "guard", "mode": "SAFE", "prompt": "x"},
        )
        assert denied.status_code == 401

        allowed = await client.post(
            "/jobs",
            json={"engine": "codex", "repo": "guard", "mode": "SAFE", "prompt": "x"},
            headers={"x-api-key": "secret"},
        )
        assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_list_engines_endpoint(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/engines")
        assert response.status_code == 200
        payload = response.json()
        names = {item["name"] for item in payload}
        assert {"codex", "cloudcode", "opencode"}.issubset(names)
        codex = next(item for item in payload if item["name"] == "codex")
        assert codex["capabilities"]["supports_noninteractive"] is True


@pytest.mark.asyncio
async def test_health_ready_metrics_endpoints(tmp_path: Path):
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        health_payload = health.json()
        assert health_payload["status"] == "ok"
        assert health_payload["uptime_seconds"] >= 0

        ready = await client.get("/ready")
        assert ready.status_code == 200
        ready_payload = ready.json()
        assert ready_payload["status"] == "ready"
        assert ready_payload["db_ok"] is True
        assert ready_payload["disk_ok"] is True
        assert ready_payload["free_disk_mb"] > 0

        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert "pocketcoder_uptime_seconds" in metrics.text
        assert "pocketcoder_jobs_total" in metrics.text
        assert "pocketcoder_disk_free_megabytes" in metrics.text


@pytest.mark.asyncio
async def test_readiness_and_create_job_fail_on_low_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("POCKETCODER_MIN_FREE_DISK_MB", "1024")
    disk_tuple = type("DiskUsage", (), {})

    def fake_disk_usage(_: Path):
        result = disk_tuple()
        result.total = 10 * 1024 * 1024
        result.used = 9 * 1024 * 1024
        result.free = 16 * 1024
        return result

    monkeypatch.setattr("pocketcoder_daemon.manager.shutil.disk_usage", fake_disk_usage)

    app = create_app(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ready = await client.get("/ready")
        assert ready.status_code == 503

        create = await client.post(
            "/jobs",
            json={"engine": "codex", "repo": "diskguard", "mode": "SAFE", "prompt": "run"},
        )
        assert create.status_code == 409
        assert "Insufficient free disk space" in create.json()["detail"]


@pytest.mark.asyncio
async def test_cli_tools_install_update_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POCKETCODER_CODEX_INSTALL_CMD", "python3 -c \"print('install codex ok')\"")
    monkeypatch.setenv("POCKETCODER_CODEX_UPDATE_CMD", "python3 -c \"print('update codex ok')\"")

    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/cli/tools")
        assert listed.status_code == 200
        tools = listed.json()
        assert any(tool["name"] == "codex" for tool in tools)

        install = await client.post("/cli/tools/codex/install")
        assert install.status_code == 200
        install_payload = install.json()
        assert install_payload["name"] == "codex"
        assert install_payload["action"] == "install"
        assert install_payload["status"] == "ok"

        update = await client.post("/cli/tools/codex/update")
        assert update.status_code == 200
        update_payload = update.json()
        assert update_payload["name"] == "codex"
        assert update_payload["action"] == "update"
        assert update_payload["status"] == "ok"

        bad = await client.post("/cli/tools/unknown/install")
        assert bad.status_code == 400


@pytest.mark.asyncio
async def test_codex_engine_runs_with_configured_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "POCKETCODER_CODEX_SAFE_COMMAND_JSON",
        json.dumps(
            [
                "python3",
                "-c",
                "import sys; print('codex safe prompt=' + sys.argv[-1])",
                "{prompt}",
            ]
        ),
    )
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "codex",
                "repo": "codex-repo",
                "mode": "SAFE",
                "prompt": "implement parser",
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        detail = None
        for _ in range(100):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}:
                break
            await asyncio.sleep(0.02)

        assert detail is not None
        assert detail["status"] == "COMPLETED"

    stdout_log_gz = tmp_path / "logs" / f"job-{job_id}.stdout.log.gz"
    assert stdout_log_gz.exists()
    output = gzip.decompress(stdout_log_gz.read_bytes()).decode()
    assert "codex safe prompt=implement parser" in output


@pytest.mark.asyncio
async def test_cloudcode_engine_runs_with_configured_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "POCKETCODER_CLOUDCODE_SAFE_COMMAND_JSON",
        json.dumps(
            [
                "python3",
                "-c",
                "import sys; print('cloudcode safe prompt=' + sys.argv[-1])",
                "{prompt}",
            ]
        ),
    )
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "cloudcode",
                "repo": "cloudcode-repo",
                "mode": "SAFE",
                "prompt": "build feature",
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        detail = None
        for _ in range(100):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}:
                break
            await asyncio.sleep(0.02)

        assert detail is not None
        assert detail["status"] == "COMPLETED"

    stdout_log_gz = tmp_path / "logs" / f"job-{job_id}.stdout.log.gz"
    assert stdout_log_gz.exists()
    output = gzip.decompress(stdout_log_gz.read_bytes()).decode()
    assert "cloudcode safe prompt=build feature" in output


@pytest.mark.asyncio
async def test_stdout_log_rotation_creates_multiple_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("POCKETCODER_LOG_MAX_BYTES", "100")
    monkeypatch.setenv("POCKETCODER_LOG_BACKUP_COUNT", "2")
    monkeypatch.setenv(
        "POCKETCODER_CODEX_SAFE_COMMAND_JSON",
        json.dumps(
            [
                "python3",
                "-c",
                (
                    "import sys; "
                    "[print('line-' + str(i) + '-' + ('x'*40), flush=True) for i in range(20)]"
                ),
                "{prompt}",
            ]
        ),
    )
    app = create_app(tmp_path)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/jobs",
            json={
                "engine": "codex",
                "repo": "rotate-repo",
                "mode": "SAFE",
                "prompt": "rotate",
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        detail = None
        for _ in range(120):
            detail = (await client.get(f"/jobs/{job_id}")).json()
            if detail["status"] in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}:
                break
            await asyncio.sleep(0.02)

        assert detail is not None
        assert detail["status"] == "COMPLETED"

    stdout_artifacts = [path for path in detail["artifacts"] if "stdout" in path]
    assert len(stdout_artifacts) >= 2
    assert any(path.endswith(".stdout.log.1.gz") for path in stdout_artifacts)
