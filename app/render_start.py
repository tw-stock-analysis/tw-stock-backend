"""Single-instance Render startup: migrate successfully before serving traffic."""

import os
import subprocess
import sys

from app.core.config import BACKEND_ROOT


def port(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")
    return str(int(value))


def main() -> None:
    listen_port = port(os.environ.get("PORT", "10000"))
    # Do not continue serving a schema the application cannot use. The process
    # receives DATABASE_URL from Render; no credentials are placed in arguments.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(BACKEND_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        check=True,
        cwd=BACKEND_ROOT,
    )
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            listen_port,
            "--proxy-headers",
        ],
    )


if __name__ == "__main__":
    main()
