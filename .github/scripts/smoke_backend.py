"""Exercise the built runtime image with an isolated, disposable PostgreSQL."""

import argparse
import json
import os
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    image = parser.parse_args().image
    prefix = f"twstock-ci-{uuid4().hex[:12]}"
    database = f"{prefix}-db"
    backend = f"{prefix}-api"
    env = os.environ | {
        "APP_ENV": "production",
        "AUTH_ALLOWED_ORIGINS": '["https://ci.example.test"]',
        "POSTGRES_USER": "ci_user",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "POSTGRES_DB": "twstock_ci",
        "POSTGRES_HOST": database,
        "POSTGRES_PORT": "5432",
        "DATABASE_URL": "",
    }

    def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", *args], env=env, capture_output=True, text=True, timeout=120
        )
        if check and result.returncode:
            raise RuntimeError(f"Docker command failed: {result.stderr}")
        return result

    runtime_env = []
    for key in (
        "APP_ENV",
        "AUTH_ALLOWED_ORIGINS",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "DATABASE_URL",
    ):
        runtime_env.extend(["--env", key])
    opener = build_opener(ProxyHandler({}))

    def get(path: str) -> tuple[int, dict]:
        try:
            response = opener.open(base + path, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def wait_for(path: str, expected: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                status, body = get(path)
                if status == 200 and body.get("data", {}).get("status") == expected:
                    return
            except (OSError, URLError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError(f"Timed out waiting for {path}")

    try:
        docker("network", "create", prefix)
        docker(
            "run",
            "--detach",
            "--name",
            database,
            "--network",
            prefix,
            "--env",
            "POSTGRES_USER",
            "--env",
            "POSTGRES_PASSWORD",
            "--env",
            "POSTGRES_DB",
            "postgres:17",
        )
        deadline = time.monotonic() + 60
        while True:
            ready = docker(
                "exec",
                database,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "ci_user",
                "-d",
                "twstock_ci",
                check=False,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL did not become ready")
            time.sleep(1)
        docker(
            "run",
            "--detach",
            "--name",
            backend,
            "--network",
            prefix,
            "--publish",
            "127.0.0.1::8000",
            *runtime_env,
            image,
        )
        port = docker("port", backend, "8000/tcp").stdout.strip().rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        wait_for("/api/v1/health/live", "ok")
        status, body = get("/api/v1/health/ready")
        assert status == 503 and body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        print("Readiness rejects an unmigrated database", flush=True)
        for operation in (("upgrade", "head"), ("check",)):
            docker("run", "--rm", "--network", prefix, *runtime_env, image, "alembic", *operation)
        wait_for("/api/v1/health/ready", "ready")
        assert get("/api/v1/me")[0] == 401
        assert get("/api/v1/unknown")[0] == 404
        assert get("/api/v1/docs")[0] == 404
        status, catalog = get("/api/v1/twse/datasets")
        assert status == 200 and catalog["data"]
        print("Runtime image: migration/check, readiness, auth and catalog passed", flush=True)
    except BaseException:
        for container in (backend, database):
            result = docker("logs", "--tail", "100", container, check=False)
            print(result.stdout)
            print(result.stderr)
        raise
    finally:
        docker("rm", "--force", "--volumes", backend, database, check=False)
        docker("network", "rm", prefix, check=False)


if __name__ == "__main__":
    main()
