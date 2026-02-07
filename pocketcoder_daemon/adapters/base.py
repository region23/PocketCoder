from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import os
import signal
from typing import AsyncIterator, Literal

from pocketcoder_daemon.models import JobStatus


@dataclass(frozen=True)
class Capabilities:
    supports_json_events: bool
    supports_noninteractive: bool
    supports_yolo: bool
    requires_pty: bool


@dataclass(frozen=True)
class EngineEvent:
    kind: Literal["stdout", "stderr", "input_required", "eof"]
    text: str


@dataclass(frozen=True)
class EngineProcess:
    process: asyncio.subprocess.Process
    command: list[str]


class EngineAdapter:
    name: str

    def detect_capabilities(self) -> Capabilities:
        raise NotImplementedError

    def build_command(self, prompt: str, mode: str) -> list[str]:
        raise NotImplementedError

    async def start_process(
        self,
        prompt: str,
        mode: str,
        cwd: Path | None = None,
    ) -> EngineProcess:
        command = self.build_command(prompt, mode)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        return EngineProcess(process=process, command=command)

    async def stream_events(self, handle: EngineProcess) -> AsyncIterator[EngineEvent]:
        queue: asyncio.Queue[EngineEvent] = asyncio.Queue()

        async def pump(
            stream: asyncio.StreamReader | None,
            kind: Literal["stdout", "stderr"],
        ) -> None:
            if stream is None:
                await queue.put(EngineEvent("eof", kind))
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace")
                if kind == "stdout" and text.startswith("INPUT_REQUIRED:"):
                    prompt = text.split(":", 1)[1].strip() or "Input required"
                    await queue.put(EngineEvent("input_required", prompt))
                    continue
                await queue.put(EngineEvent(kind, text))
            await queue.put(EngineEvent("eof", kind))

        tasks = [
            asyncio.create_task(pump(handle.process.stdout, "stdout")),
            asyncio.create_task(pump(handle.process.stderr, "stderr")),
        ]
        try:
            eof_count = 0
            while eof_count < 2:
                event = await queue.get()
                if event.kind == "eof":
                    eof_count += 1
                    continue
                yield event
            await handle.process.wait()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_input(self, handle: EngineProcess, text: str) -> None:
        if handle.process.stdin is None:
            raise RuntimeError("Engine process does not accept stdin")
        handle.process.stdin.write(f"{text}\n".encode())
        await handle.process.stdin.drain()

    async def cancel(self, handle: EngineProcess, grace_seconds: float = 0.5) -> None:
        process = handle.process
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            return

    def summarize(
        self,
        returncode: int,
        *,
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> JobStatus:
        if cancelled:
            return JobStatus.CANCELLED
        if timed_out:
            return JobStatus.TIMEOUT
        return JobStatus.COMPLETED if returncode == 0 else JobStatus.FAILED
