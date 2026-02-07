from pathlib import Path

import uvicorn

from pocketcoder_daemon.app import create_app


def run() -> None:
    root = Path.cwd()
    app = create_app(root)
    uvicorn.run(app, host="127.0.0.1", port=8080)


if __name__ == "__main__":
    run()
