from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import json
import os
import pty
import signal
import subprocess
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
    options: list[str] | None = None


@dataclass
class EngineProcess:
    command: list[str]
    async_process: asyncio.subprocess.Process | None = None
    sync_process: subprocess.Popen | None = None
    pty_master_fd: int | None = None

    @property
    def pid(self) -> int:
        if self.async_process is not None:
            return self.async_process.pid
        if self.sync_process is not None:
            return self.sync_process.pid
        raise RuntimeError("Engine process is not initialized")

    @property
    def returncode(self) -> int | None:
        if self.async_process is not None:
            return self.async_process.returncode
        if self.sync_process is not None:
            return self.sync_process.returncode
        return None


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
        capabilities = self.detect_capabilities()
        command = self.build_command(prompt, mode)
        if capabilities.requires_pty:
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(cwd) if cwd else None,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=os.setsid,
                    close_fds=True,
                )
            except Exception:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
            return EngineProcess(
                command=command,
                sync_process=process,
                pty_master_fd=master_fd,
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        return EngineProcess(command=command, async_process=process)

    async def stream_events(self, handle: EngineProcess) -> AsyncIterator[EngineEvent]:
        if handle.pty_master_fd is not None:
            async for event in self._stream_events_pty(handle):
                yield event
            return

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
                event = self._to_event(kind, text)
                if event is not None:
                    await queue.put(event)
            await queue.put(EngineEvent("eof", kind))

        tasks = [
            asyncio.create_task(
                pump(handle.async_process.stdout if handle.async_process else None, "stdout")
            ),
            asyncio.create_task(
                pump(handle.async_process.stderr if handle.async_process else None, "stderr")
            ),
        ]
        try:
            eof_count = 0
            while eof_count < 2:
                event = await queue.get()
                if event.kind == "eof":
                    eof_count += 1
                    continue
                yield event
            await self.wait(handle)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_input(self, handle: EngineProcess, text: str) -> None:
        if handle.pty_master_fd is not None:
            await asyncio.to_thread(os.write, handle.pty_master_fd, f"{text}\n".encode())
            return
        if handle.async_process is None or handle.async_process.stdin is None:
            raise RuntimeError("Engine process does not accept stdin")
        handle.async_process.stdin.write(f"{text}\n".encode())
        await handle.async_process.stdin.drain()

    async def cancel(self, handle: EngineProcess, grace_seconds: float = 0.5) -> None:
        try:
            os.killpg(os.getpgid(handle.pid), signal.SIGTERM)
            await asyncio.wait_for(self.wait(handle), timeout=grace_seconds)
        except asyncio.TimeoutError:
            os.killpg(os.getpgid(handle.pid), signal.SIGKILL)
            await self.wait(handle)
        except ProcessLookupError:
            return

    async def wait(self, handle: EngineProcess) -> None:
        if handle.async_process is not None:
            await handle.async_process.wait()
            return
        if handle.sync_process is not None:
            await asyncio.to_thread(handle.sync_process.wait)
            return
        raise RuntimeError("Engine process is not initialized")

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

    async def _stream_events_pty(self, handle: EngineProcess) -> AsyncIterator[EngineEvent]:
        master_fd = handle.pty_master_fd
        if master_fd is None:
            return
        buffer = b""
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, master_fd, 1024)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", maxsplit=1)
                    text = (raw_line + b"\n").decode(errors="replace")
                    event = self._to_event("stdout", text)
                    if event is not None:
                        yield event
            if buffer:
                text = buffer.decode(errors="replace")
                event = self._to_event("stdout", text)
                if event is not None:
                    yield event
            await self.wait(handle)
        finally:
            os.close(master_fd)
            handle.pty_master_fd = None

    @staticmethod
    def _to_event(kind: Literal["stdout", "stderr"], text: str) -> EngineEvent | None:
        if kind == "stdout" and text.startswith("INPUT_REQUIRED_JSON:"):
            raw = text.split(":", 1)[1].strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            prompt = payload.get("prompt") if isinstance(payload, dict) else None
            options_raw = payload.get("options") if isinstance(payload, dict) else None
            options: list[str] = []
            if isinstance(options_raw, list):
                for item in options_raw:
                    if isinstance(item, str):
                        options.append(item)
            return EngineEvent(
                "input_required",
                prompt if isinstance(prompt, str) and prompt else "Input required",
                options=options,
            )
        if kind == "stdout" and text.startswith("INPUT_REQUIRED:"):
            prompt = text.split(":", 1)[1].strip() or "Input required"
            return EngineEvent("input_required", prompt, options=[])
        return EngineEvent(kind, text)
