from __future__ import annotations

from types import SimpleNamespace

import pytest

from pocketcoder_bot.daemon_client import DaemonAPIError
from pocketcoder_bot import main as bot_main


def _job_payload(
    *,
    job_id: int,
    status: str,
    chat_id: int | None,
    input_prompt: str | None = None,
    input_options: list[str] | None = None,
) -> dict:
    return {
        "id": job_id,
        "status": status,
        "engine": "codex",
        "repo": "demo",
        "mode": "SAFE",
        "branch": f"pc/demo/{job_id}",
        "created_at": "2026-02-07T00:00:00+00:00",
        "started_at": "2026-02-07T00:00:01+00:00",
        "finished_at": None,
        "requester_chat_id": chat_id,
        "input_prompt": input_prompt,
        "input_options": input_options or [],
    }


class _FakeDaemonClient:
    def __init__(self, responses: list[list[dict]]):
        self._responses = responses
        self._calls = 0

    async def list_jobs(self) -> list[dict]:
        if not self._responses:
            return []
        idx = min(self._calls, len(self._responses) - 1)
        self._calls += 1
        return self._responses[idx]


class _FailingDaemonClient:
    async def list_jobs(self) -> list[dict]:
        raise DaemonAPIError("boom")


class _DummyBot:
    def __init__(self):
        self.sent_messages: list[dict] = []

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None):
        self.sent_messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )


class _DummyContext:
    def __init__(self):
        self.bot = _DummyBot()
        self.application = SimpleNamespace(bot_data={})


@pytest.mark.asyncio
async def test_poll_job_updates_notifies_waiting_input_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_job = _job_payload(
        job_id=11,
        status="WAITING_INPUT",
        chat_id=777,
        input_prompt="Pick option",
        input_options=["a", "b"],
    )
    client = _FakeDaemonClient([[waiting_job], [waiting_job]])
    monkeypatch.setattr(bot_main, "_daemon_client", lambda: client)
    context = _DummyContext()

    await bot_main.poll_job_updates(context)
    assert len(context.bot.sent_messages) == 1
    sent = context.bot.sent_messages[0]
    assert sent["chat_id"] == 777
    assert "waiting input" in sent["text"]

    await bot_main.poll_job_updates(context)
    assert len(context.bot.sent_messages) == 1


@pytest.mark.asyncio
async def test_poll_job_updates_notifies_on_lost_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_job = _job_payload(job_id=42, status="RUNNING", chat_id=555)
    lost_job = _job_payload(job_id=42, status="LOST", chat_id=555)
    client = _FakeDaemonClient([[running_job], [lost_job]])
    monkeypatch.setattr(bot_main, "_daemon_client", lambda: client)
    context = _DummyContext()

    await bot_main.poll_job_updates(context)
    assert context.bot.sent_messages == []

    await bot_main.poll_job_updates(context)
    assert len(context.bot.sent_messages) == 1
    sent = context.bot.sent_messages[0]
    assert sent["chat_id"] == 555
    assert "marked as LOST" in sent["text"]


@pytest.mark.asyncio
async def test_poll_job_updates_ignores_daemon_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_main, "_daemon_client", lambda: _FailingDaemonClient())
    context = _DummyContext()

    await bot_main.poll_job_updates(context)
    assert context.bot.sent_messages == []
