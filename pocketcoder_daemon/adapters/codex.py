from __future__ import annotations

import json
import os
import shlex
from typing import Sequence

from pocketcoder_daemon.adapters.base import Capabilities, EngineAdapter
from pocketcoder_daemon.models import JobMode


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class ConfigurableCLIAdapter(EngineAdapter):
    name: str
    env_prefix: str
    default_bin: str
    default_safe_args: str
    default_yolo_args: str

    def detect_capabilities(self) -> Capabilities:
        prefix = self.env_prefix
        return Capabilities(
            supports_json_events=_env_bool(f"POCKETCODER_{prefix}_SUPPORTS_JSON_EVENTS", False),
            supports_noninteractive=_env_bool(
                f"POCKETCODER_{prefix}_SUPPORTS_NONINTERACTIVE", True
            ),
            supports_yolo=_env_bool(f"POCKETCODER_{prefix}_SUPPORTS_YOLO", True),
            requires_pty=_env_bool(f"POCKETCODER_{prefix}_REQUIRES_PTY", False),
        )

    def build_command(self, prompt: str, mode: str) -> list[str]:
        command = self._command_template(mode)
        return self._inject_prompt(command, prompt)

    def _command_template(self, mode: str) -> list[str]:
        prefix = self.env_prefix
        normalized_mode = str(mode).upper()
        if normalized_mode == JobMode.YOLO:
            json_key = f"POCKETCODER_{prefix}_YOLO_COMMAND_JSON"
            args_key = f"POCKETCODER_{prefix}_YOLO_ARGS"
            default_args = self.default_yolo_args
        else:
            json_key = f"POCKETCODER_{prefix}_SAFE_COMMAND_JSON"
            args_key = f"POCKETCODER_{prefix}_SAFE_ARGS"
            default_args = self.default_safe_args

        raw_json = os.getenv(json_key)
        if raw_json:
            return self._parse_json_command(raw_json, env_name=json_key)

        binary = os.getenv(f"POCKETCODER_{prefix}_BIN", self.default_bin)
        raw_args = os.getenv(args_key, default_args)
        return [binary, *shlex.split(raw_args)]

    @staticmethod
    def _inject_prompt(command: Sequence[str], prompt: str) -> list[str]:
        rendered: list[str] = []
        replaced = False
        for part in command:
            if "{prompt}" in part:
                rendered.append(part.replace("{prompt}", prompt))
                replaced = True
            else:
                rendered.append(part)
        if not replaced:
            rendered.append(prompt)
        return rendered

    @staticmethod
    def _parse_json_command(raw: str, *, env_name: str) -> list[str]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{env_name} must be valid JSON array") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{env_name} must be a JSON array of strings")
        if not parsed:
            raise ValueError(f"{env_name} cannot be empty")
        return list(parsed)


class CodexAdapter(ConfigurableCLIAdapter):
    name = "codex"
    env_prefix = "CODEX"
    default_bin = "codex"
    default_safe_args = "exec --mode safe"
    default_yolo_args = "exec --mode yolo"


class CloudCodeAdapter(ConfigurableCLIAdapter):
    name = "cloudcode"
    env_prefix = "CLOUDCODE"
    default_bin = "cloudcode"
    default_safe_args = "exec --mode safe"
    default_yolo_args = "exec --mode yolo"


class OpenCodeAdapter(ConfigurableCLIAdapter):
    name = "opencode"
    env_prefix = "OPENCODE"
    default_bin = "opencode"
    default_safe_args = "exec --mode safe"
    default_yolo_args = "exec --mode yolo"

