"""Verify the production Compose and HTTPS auth using only disposable CI resources."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    project = f"twstock-deploy-ci-{uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        http_port, https_port = free_port(), free_port()
        origin = f"https://ci.example.test:{https_port}"
        certificate, key = directory / "fullchain.pem", directory / "privkey.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=ci.example.test",
                "-addext",
                "subjectAltName=DNS:ci.example.test",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ],
            check=True,
            capture_output=True,
        )
        profile = directory / ".env.production"
        profile.write_text(
            "BACKEND_IMAGE=ghcr.io/tw-stock-analysis/tw-stock-backend@sha256:" + "a" * 64 + "\n"
            "FRONTEND_IMAGE=ghcr.io/tw-stock-analysis/tw-stock-frontend@sha256:" + "b" * 64 + "\n"
            f'SITE_HOST=ci.example.test\nAUTH_ALLOWED_ORIGINS=["{origin}"]\n'
            f"TLS_CERT_DIR={directory}\nPOSTGRES_USER=ci_user\nPOSTGRES_DB=ci_deploy\n"
            f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\nWEB_BIND_ADDRESS=127.0.0.1\n"
            f"WEB_HTTP_PORT={http_port}\nWEB_HTTPS_PORT={https_port}\n"
        )
        profile.chmod(0o600)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/deploy.py"),
                "--env-file",
                str(profile),
                "--check",
            ],
            cwd=ROOT,
            check=True,
        )
        # Check the real Compose topology using already-built local images, not placeholder digests.
        override = directory / "images.yml"
        override.write_text(
            "services:\n"
            "  backend:\n    image: tw-stock-backend:ci\n"
            "  migrate:\n    image: tw-stock-backend:ci\n"
            "  web:\n    image: tw-stock-frontend:deployment-check\n"
        )
        env = os.environ | {"DEPLOY_ENV_FILE": str(profile)}
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(profile),
            "-f",
            str(ROOT / "docker-compose.deploy.yml"),
            "-f",
            str(override),
        ]

        def run(*args: str, **kwargs):
            return subprocess.run(
                list(args),
                env=env,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=180,
                **kwargs,
            )

        def request(path: str, *, method: str = "GET", data: dict | None = None):
            curl = [
                "curl",
                "--silent",
                "--show-error",
                "--noproxy",
                "*",
                "--cacert",
                str(certificate),
                "--resolve",
                f"ci.example.test:{https_port}:127.0.0.1",
                "--max-time",
                "15",
                "--cookie",
                str(directory / "cookies"),
                "--cookie-jar",
                str(directory / "cookies"),
                "--dump-header",
                str(directory / "headers"),
                "--write-out",
                "\n%{http_code}",
                "--request",
                method,
            ]
            if method == "POST":
                curl += [
                    "--header",
                    f"Origin: {origin}",
                    "--header",
                    "X-CSRF-Protection: 1",
                    "--header",
                    "Content-Type: application/json",
                ]
            if data is not None:
                curl += ["--data-binary", "@-"]
            response = run(
                *curl, origin + path, input=json.dumps(data) if data else None, check=True
            )
            body, status = response.stdout.rsplit("\n", 1)
            return int(status), body

        try:
            result = run(*compose, "up", "-d", "--wait", "--wait-timeout", "120", check=True)
            print(result.stderr)
            assert request("/api/v1/health/ready")[0] == 200
            assert request("/login")[0] == 200
            assert request("/account/sessions")[0] == 200
            assert request("/api/v1/docs")[0] == 404
            redirect = run(
                "curl",
                "--silent",
                "--show-error",
                "--noproxy",
                "*",
                "--max-time",
                "10",
                "--header",
                "Host: ci.example.test",
                "--dump-header",
                "-",
                "--output",
                "/dev/null",
                f"http://127.0.0.1:{http_port}/login",
                check=True,
            ).stdout.lower()
            assert "308" in redirect and f"location: {origin}/login" in redirect
            credentials = {
                "email": f"deploy-{uuid4().hex}@example.test",
                "password": secrets.token_urlsafe(24),
            }
            code = """import asyncio, json
from datetime import timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.core.config import get_settings
from app.core.passwords import PasswordService
from app.services.accounts import AccountService
from app.services.credentials import VerificationService
credentials = json.loads(CREDENTIALS)
async def main():
    engine = create_async_engine(get_settings().sqlalchemy_url())
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        accounts = AccountService(factory)
        password_hash = await PasswordService().hash(credentials['password'])
        user = await accounts.create(credentials['email'], password_hash=password_hash)
        token = await VerificationService(factory).issue(user, 'verify_email', timedelta(minutes=5))
        await accounts.verify_email(token.value)
    finally:
        await engine.dispose()
asyncio.run(main())
""".replace("CREDENTIALS", repr(json.dumps(credentials)))
            run(*compose, "exec", "-T", "backend", "python", "-", input=code, check=True)
            assert request("/api/v1/auth/login", method="POST", data=credentials)[0] == 200
            cookie_headers = (directory / "headers").read_text().lower()
            assert "__host-session=" in cookie_headers
            assert "secure" in cookie_headers and "httponly" in cookie_headers
            assert request("/api/v1/me")[0] == 200
            assert request("/api/v1/auth/logout", method="POST")[0] == 204
            assert request("/api/v1/me")[0] == 401
            print(
                "Production Compose: TLS, redirect, migrations, Secure Cookie login/logout passed"
            )
        except BaseException:
            result = run(*compose, "logs", "--no-color", "--tail", "100")
            print(result.stdout)
            print(result.stderr)
            raise
        finally:
            # Only this uniquely named test project is removed, including its test DB volume.
            run(*compose, "down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
