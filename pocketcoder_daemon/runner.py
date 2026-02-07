from __future__ import annotations

import asyncio
from dataclasses import dataclass
import gzip
import os
from pathlib import Path
import subprocess

from pocketcoder_daemon.adapters.base import EngineAdapter, EngineProcess
from pocketcoder_daemon.db import JobStore
from pocketcoder_daemon.models import JobStatus, utcnow


@dataclass
class ActiveRun:
    adapter: EngineAdapter
    handle: EngineProcess


@dataclass(frozen=True)
class LogRotationConfig:
    max_bytes: int
    backup_count: int


class ExecutionRunner:
    def __init__(
        self,
        store: JobStore,
        logs_dir: Path,
        *,
        log_max_bytes: int | None = None,
        log_backup_count: int | None = None,
    ):
        self.store = store
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[int, asyncio.Task] = {}
        self.active_runs: dict[int, ActiveRun] = {}
        resolved_max_bytes = (
            self._env_int("POCKETCODER_LOG_MAX_BYTES", 5 * 1024 * 1024)
            if log_max_bytes is None
            else log_max_bytes
        )
        resolved_backup_count = (
            self._env_int("POCKETCODER_LOG_BACKUP_COUNT", 3)
            if log_backup_count is None
            else log_backup_count
        )
        self.log_rotation = LogRotationConfig(
            max_bytes=max(0, int(resolved_max_bytes)),
            backup_count=max(0, int(resolved_backup_count)),
        )

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
        stdout_path.touch(exist_ok=True)
        stderr_path.touch(exist_ok=True)
        try:
            self.store.update_status(
                job_id,
                JobStatus.RUNNING,
                started_at=utcnow().isoformat(),
                input_prompt=None,
                input_options=[],
            )
            timed_out = False
            handle: EngineProcess | None = None
            stream = None
            latest_stdout_preview: str | None = None
            try:
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
                        self._append_log(stdout_path, event.text.encode())
                        preview = event.text.strip()
                        if preview:
                            latest_stdout_preview = preview[:500]
                    elif event.kind == "stderr":
                        self._append_log(stderr_path, event.text.encode())
                    elif event.kind == "input_required":
                        self.store.update_status(
                            job_id,
                            JobStatus.WAITING_INPUT,
                            input_prompt=event.text,
                            input_options=event.options or [],
                        )
            finally:
                if stream is not None and hasattr(stream, "aclose"):
                    await stream.aclose()
                if handle is not None and handle.returncode is None:
                    await adapter.wait(handle)
                self.active_runs.pop(job_id, None)

            if handle is None:
                return

            current = self.store.get(job_id)
            cancelled = current.status == JobStatus.CANCELLED
            returncode = handle.returncode
            if returncode is None:
                returncode = 1
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
                input_options=[],
                stdout_preview=latest_stdout_preview,
            )
            if status == JobStatus.COMPLETED:
                try:
                    commit_hash = await self._finalize_git(cwd, branch, job_id)
                    if commit_hash:
                        self.store.update(job_id, commit_hash=commit_hash)
                except Exception:
                    # Git post-processing is best-effort and must not flip a completed job to failed.
                    pass

            for stdout_gz in self._gzip_log_family(stdout_path):
                self.store.add_artifact(job_id, str(stdout_gz), kind="stdout_log")
            for stderr_gz in self._gzip_log_family(stderr_path):
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
            input_options=[],
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
        self.store.update_status(job_id, JobStatus.RUNNING, input_prompt=None, input_options=[])

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

    def _gzip_log_family(self, base_path: Path) -> list[Path]:
        gz_paths: list[Path] = []
        for segment in self._collect_log_segments(base_path):
            gz_paths.append(self._gzip_log(segment))
        return gz_paths

    def _collect_log_segments(self, base_path: Path) -> list[Path]:
        segments: list[Path] = []
        for idx in range(self.log_rotation.backup_count, 0, -1):
            candidate = base_path.with_name(f"{base_path.name}.{idx}")
            if candidate.exists():
                segments.append(candidate)
        if base_path.exists():
            segments.append(base_path)
        return segments

    def _append_log(self, path: Path, chunk: bytes) -> None:
        if not chunk:
            return
        self._rotate_if_needed(path, len(chunk))
        with path.open("ab") as log_file:
            log_file.write(chunk)

    def _rotate_if_needed(self, path: Path, incoming_bytes: int) -> None:
        if self.log_rotation.max_bytes <= 0:
            return
        current_size = path.stat().st_size if path.exists() else 0
        if current_size + incoming_bytes <= self.log_rotation.max_bytes:
            return
        self._rotate(path)

    def _rotate(self, path: Path) -> None:
        backup_count = self.log_rotation.backup_count
        if backup_count <= 0:
            path.unlink(missing_ok=True)
            return
        oldest = path.with_name(f"{path.name}.{backup_count}")
        oldest.unlink(missing_ok=True)
        for idx in range(backup_count - 1, 0, -1):
            src = path.with_name(f"{path.name}.{idx}")
            dst = path.with_name(f"{path.name}.{idx + 1}")
            if src.exists():
                src.replace(dst)
        if path.exists():
            path.replace(path.with_name(f"{path.name}.1"))

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

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return int(raw.strip() or str(default))
        except ValueError:
            return default
