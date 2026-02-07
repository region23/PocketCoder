from pathlib import Path
import os

import uvicorn

from pocketcoder_daemon.app import create_app


def run() -> None:
    root = Path(os.getenv("POCKETCODER_ROOT", Path.cwd()))
    app = create_app(root)
    host = os.getenv("POCKETCODER_HOST", "127.0.0.1")
    port = int(os.getenv("POCKETCODER_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
