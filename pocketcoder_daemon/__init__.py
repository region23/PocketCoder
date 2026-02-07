from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(root: Path) -> "FastAPI":
    # Lazy import keeps non-HTTP modules usable without FastAPI installed.
    from pocketcoder_daemon.app import create_app as _create_app

    return _create_app(root)


__all__ = ["create_app"]
