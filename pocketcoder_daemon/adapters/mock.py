from __future__ import annotations

from pocketcoder_daemon.adapters.base import Capabilities, EngineAdapter


class MockAdapter(EngineAdapter):
    name = "mock"

    def detect_capabilities(self) -> Capabilities:
        return Capabilities(True, True, True, False)

    def build_command(self, prompt: str, mode: str) -> list[str]:
        return [
            "python3",
            "-c",
            f"print('mock engine: {mode}: {prompt}')",
        ]


class SleepyAdapter(EngineAdapter):
    name = "sleepy"

    def detect_capabilities(self) -> Capabilities:
        return Capabilities(False, True, False, False)

    def build_command(self, prompt: str, mode: str) -> list[str]:
        return [
            "python3",
            "-c",
            "import time; print('started sleepy'); time.sleep(0.6); print('finished sleepy')",
        ]
