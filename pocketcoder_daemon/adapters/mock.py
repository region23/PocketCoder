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


class InteractiveAdapter(EngineAdapter):
    name = "interactive"

    def detect_capabilities(self) -> Capabilities:
        return Capabilities(False, False, False, True)

    def build_command(self, prompt: str, mode: str) -> list[str]:
        return [
            "python3",
            "-c",
            (
                "import sys; "
                "print(f'tty stdin={sys.stdin.isatty()} stdout={sys.stdout.isatty()}', flush=True); "
                "print('INPUT_REQUIRED_JSON:{\"prompt\":\"Choose an option\",\"options\":[\"option-1\",\"option-2\"]}', flush=True); "
                "answer = sys.stdin.readline().strip(); "
                "print(f'received: {answer}', flush=True)"
            ),
        ]
