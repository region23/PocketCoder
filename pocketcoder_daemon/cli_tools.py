from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import shlex
import shutil
import subprocess

from pocketcoder_daemon.models import CLIActionResult, CLIToolStatus, utcnow


@dataclass(frozen=True)
class CLIToolSpec:
    name: str
    env_prefix: str
    default_bin: str
    default_version_args: str = "--version"


class CLIToolError(Exception):
    pass


class CLIToolManager:
    def __init__(self) -> None:
        self.specs = {
            "codex": CLIToolSpec("codex", "CODEX", "codex"),
            "cloudcode": CLIToolSpec("cloudcode", "CLOUDCODE", "cloudcode"),
            "opencode": CLIToolSpec("opencode", "OPENCODE", "opencode"),
        }
        self._last_actions: dict[str, CLIActionResult] = {}
        self._action_lock = asyncio.Lock()

    def list_tools(self) -> list[CLIToolStatus]:
        result: list[CLIToolStatus] = []
        for name in sorted(self.specs):
            result.append(self._tool_status(name))
        return result

    async def install_tool(self, name: str) -> CLIActionResult:
        return await self._run_action(name, "install")

    async def update_tool(self, name: str) -> CLIActionResult:
        return await self._run_action(name, "update")

    async def install_missing(self) -> list[CLIActionResult]:
        results: list[CLIActionResult] = []
        for status in self.list_tools():
            if not status.available:
                results.append(await self.install_tool(status.name))
        return results

    async def update_all(self) -> list[CLIActionResult]:
        results: list[CLIActionResult] = []
        for name in sorted(self.specs):
            results.append(await self.update_tool(name))
        return results

    async def _run_action(self, name: str, action: str) -> CLIActionResult:
        spec = self.specs.get(name)
        if spec is None:
            raise CLIToolError(f"Unknown CLI tool: {name}")
        action_script = self._action_script(spec, action)
        if not action_script:
            result = CLIActionResult(
                name=name,
                action=action,
                status="error",
                message=f"{action} command is not configured",
                at=utcnow(),
            )
            self._last_actions[name] = result
            return result

        command = self._shell_command(action_script)
        async with self._action_lock:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        message = completed.stderr.strip() or completed.stdout.strip() or "ok"
        status = "ok" if completed.returncode == 0 else "error"
        result = CLIActionResult(
            name=name,
            action=action,
            status=status,
            message=message,
            at=utcnow(),
        )
        self._last_actions[name] = result
        return result

    def _tool_status(self, name: str) -> CLIToolStatus:
        spec = self.specs[name]
        binary = self._binary(spec)
        resolved_path = shutil.which(binary)
        available = resolved_path is not None
        version = self._detect_version(binary, spec) if available else None
        last = self._last_actions.get(name)
        return CLIToolStatus(
            name=name,
            binary=binary,
            resolved_path=resolved_path,
            available=available,
            version=version,
            install_command_configured=self._action_script(spec, "install") is not None,
            update_command_configured=self._action_script(spec, "update") is not None,
            last_action=last.action if last else None,
            last_action_status=last.status if last else None,
            last_action_message=last.message if last else None,
            last_action_at=last.at if last else None,
        )

    def _binary(self, spec: CLIToolSpec) -> str:
        return os.getenv(f"POCKETCODER_{spec.env_prefix}_BIN", spec.default_bin)

    def _version_args(self, spec: CLIToolSpec) -> list[str]:
        raw = os.getenv(
            f"POCKETCODER_{spec.env_prefix}_VERSION_ARGS",
            spec.default_version_args,
        )
        return shlex.split(raw)

    def _action_script(self, spec: CLIToolSpec, action: str) -> str | None:
        raw = os.getenv(f"POCKETCODER_{spec.env_prefix}_{action.upper()}_CMD")
        if not raw:
            return None
        binary = self._binary(spec)
        return raw.replace("{bin}", binary)

    def _shell_command(self, script: str) -> list[str]:
        # Lifecycle commands are configured by the operator and may include pipelines.
        bash = shutil.which("bash")
        if bash is not None:
            return [bash, "-o", "pipefail", "-c", script]
        shell = shutil.which("sh")
        if shell is not None:
            return [shell, "-c", script]
        raise CLIToolError("No shell found to run CLI lifecycle command")

    def _detect_version(self, binary: str, spec: CLIToolSpec) -> str | None:
        args = self._version_args(spec)
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            text = completed.stdout.strip() or completed.stderr.strip()
            if text:
                return text.splitlines()[0][:300]
        return None
