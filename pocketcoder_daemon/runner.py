from __future__ import annotations

import asyncio
from dataclasses import dataclass
import gzip
from pathlib import Path
import subprocess

from pocketcoder_daemon.adapters.base import EngineAdapter, EngineProcess
from pocketcoder_daemon.db import JobStore
from pocketcoder_daemon.models import JobStatus, utcnow


@dataclass
class ActiveRun:
    adapter: EngineAdapter
    handle: EngineProcess


class ExecutionRunner:
    def __init__(self, store: JobStore, logs_dir: Path):
        self.store = store
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[int, asyncio.Task] = {}
        self.active_runs: dict[int, ActiveRun] = {}

    async def run(
        self,
        job_id: int,
        adapter: EngineAdapter,
        prompt: str,
        mode: str,
        *,
        branch: str,
        timeout_seconds: float | None,
        cwd: Path | None = None,
    ) -> None:
        self.tasks[job_id] = asyncio.create_task(
            self._run(
                job_id,
                adapter,
                prompt,
                mode,
                branch=branch,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
            )
        )

    async def _run(
        self,
        job_id: int,
        adapter: EngineAdapter,
        prompt: str,
        mode: str,
        *,
        branch: str,
        timeout_seconds: float | None,
        cwd: Path | None = None,
    ) -> None:
        if self.store.get(job_id).status == JobStatus.CANCELLED:
            return

        stdout_path = self.logs_dir / f"job-{job_id}.stdout.log"
        stderr_path = self.logs_dir / f"job-{job_id}.stderr.log"
        try:
            self.store.update_status(
                job_id,
                JobStatus.RUNNING,
                started_at=utcnow().isoformat(),
                input_prompt=None,
            )
            timed_out = False
            handle: EngineProcess | None = None
            stream = None
            try:
                with stdout_path.open("ab") as stdout_log, stderr_path.open("ab") as stderr_log:
                    handle = await adapter.start_process(prompt, mode, cwd=cwd)
                    self.active_runs[job_id] = ActiveRun(adapter=adapter, handle=handle)
                    stream = adapter.stream_events(handle)
                    stream_iter = stream.__aiter__()
                    deadline = None
                    if timeout_seconds is not None:
                        deadline = asyncio.get_running_loop().time() + timeout_seconds
                    while True:
                        try:
                            if deadline is None:
                                event = await stream_iter.__anext__()
                            else:
                                remaining = deadline - asyncio.get_running_loop().time()
                                if remaining <= 0:
                                    raise asyncio.TimeoutError
                                event = await asyncio.wait_for(
                                    stream_iter.__anext__(),
                                    timeout=remaining,
                                )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            timed_out = True
                            await adapter.cancel(handle)
                            break

                        if event.kind == "stdout":
                            stdout_log.write(event.text.encode())
                        elif event.kind == "stderr":
                            stderr_log.write(event.text.encode())
                        elif event.kind == "input_required":
                            self.store.update_status(
                                job_id,
                                JobStatus.WAITING_INPUT,
                                input_prompt=event.text,
                            )
            finally:
                if stream is not None and hasattr(stream, "aclose"):
                    await stream.aclose()
                if handle is not None and handle.process.returncode is None:
                    await handle.process.wait()
                self.active_runs.pop(job_id, None)

            if handle is None:
                return

            current = self.store.get(job_id)
            cancelled = current.status == JobStatus.CANCELLED
            returncode = handle.process.returncode or 0
            status = adapter.summarize(
                returncode,
                timed_out=timed_out,
                cancelled=cancelled,
            )
            self.store.update_status(
                job_id,
                status,
                finished_at=utcnow().isoformat(),
                input_prompt=None,
            )
            if status == JobStatus.COMPLETED:
                try:
                    commit_hash = await self._finalize_git(cwd, branch, job_id)
                    if commit_hash:
                        self.store.update(job_id, commit_hash=commit_hash)
                except Exception:
                    # Git post-processing is best-effort and must not flip a completed job to failed.
                    pass

            stdout_gz = self._gzip_log(stdout_path)
            stderr_gz = self._gzip_log(stderr_path)
            self.store.add_artifact(job_id, str(stdout_gz), kind="stdout_log")
            self.store.add_artifact(job_id, str(stderr_gz), kind="stderr_log")
        except Exception:
            self.store.update_status(job_id, JobStatus.FAILED, finished_at=utcnow().isoformat())
            raise
        finally:
            self.tasks.pop(job_id, None)

    async def cancel(self, job_id: int) -> None:
        self.store.update_status(
            job_id,
            JobStatus.CANCELLED,
            finished_at=utcnow().isoformat(),
            input_prompt=None,
        )
        active = self.active_runs.get(job_id)
        if not active:
            return
        await active.adapter.cancel(active.handle)

    async def send_input(self, job_id: int, text: str) -> None:
        active = self.active_runs.get(job_id)
        if not active:
            raise RuntimeError("Job is not running")
        current = self.store.get(job_id)
        if current.status != JobStatus.WAITING_INPUT:
            raise RuntimeError("Job does not require input")
        await active.adapter.send_input(active.handle, text)
        self.store.update_status(job_id, JobStatus.RUNNING, input_prompt=None)

    async def shutdown(self) -> None:
        for job_id in list(self.active_runs):
            await self.cancel(job_id)
        if self.tasks:
            await asyncio.wait(self.tasks.values(), timeout=2)

    def _gzip_log(self, path: Path) -> Path:
        gz_path = path.with_suffix(path.suffix + ".gz")
        with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
            dst.write(src.read())
        path.unlink(missing_ok=True)
        return gz_path

    async def _finalize_git(self, cwd: Path | None, branch: str, job_id: int) -> str | None:
        if cwd is None or not (cwd / ".git").exists():
            return None

        await asyncio.to_thread(self._git, ["add", "-A"], cwd)
        has_changes = await asyncio.to_thread(self._git, ["diff", "--cached", "--quiet"], cwd)
        if has_changes.returncode != 0:
            await asyncio.to_thread(
                self._git,
                ["commit", "-m", f"PocketCoder job #{job_id}"],
                cwd,
                check=True,
            )

        if self._push_enabled():
            await asyncio.to_thread(self._git, ["push", "-u", "origin", branch], cwd, check=True)

        head = await asyncio.to_thread(self._git, ["rev-parse", "HEAD"], cwd, check=True)
        return head.stdout.strip() or None

    def _push_enabled(self) -> bool:
        import os

        return os.getenv("POCKETCODER_GIT_PUSH", "0") == "1"

    @staticmethod
    def _git(
        args: list[str],
        cwd: Path,
        *,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git failed")
        return result
