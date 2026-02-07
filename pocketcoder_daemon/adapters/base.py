from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    supports_json_events: bool
    supports_noninteractive: bool
    supports_yolo: bool
    requires_pty: bool


class EngineAdapter:
    name: str

    def detect_capabilities(self) -> Capabilities:
        raise NotImplementedError

    def build_command(self, prompt: str, mode: str) -> list[str]:
        raise NotImplementedError
