"""Validate and deploy pinned GHCR images on a prepared Linux Docker host."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def validate_config(config: dict) -> None:
    """Reject mutable image tags, shared dev volumes and unsafe production settings."""
    if config["name"] != "tw-stock-prod":
        raise ValueError("Production must use the separate tw-stock-prod Compose project")
    services = config["services"]
    for service, repository in (
        ("backend", "tw-stock-backend"),
        ("migrate", "tw-stock-backend"),
        ("web", "tw-stock-frontend"),
    ):
        pattern = rf"ghcr\.io/tw-stock-analysis/{repository}@sha256:[0-9a-f]{{64}}"
        if not re.fullmatch(pattern, services[service]["image"]):
            raise ValueError(f"{service} requires the verified GHCR digest, not a mutable tag")
    if services["backend"]["image"] != services["migrate"]["image"]:
        raise ValueError("Backend and migration must use the same image")
    for service in ("backend", "migrate", "db"):
        if services[service].get("ports"):
            raise ValueError(f"{service} must remain on the private Compose network")
    backend = services["backend"]["environment"]
    if (
        backend.get("APP_ENV") != "production"
        or backend.get("DATABASE_URL")
        or backend.get("POSTGRES_HOST") != "db"
    ):
        raise ValueError("Production must use the dedicated Compose database")
    database = services["db"]["environment"]
    if database.get("POSTGRES_PASSWORD") in (None, "", "GENERATE_WITH_SETUP"):
        raise ValueError("Replace the production PostgreSQL password placeholder")
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        if any(
            services[s]["environment"].get(key) != database.get(key) for s in ("backend", "migrate")
        ):
            raise ValueError("API, migration and database credentials must match")
    volume = config["volumes"]["production_postgres_data"]
    if volume["name"] != "tw-stock-prod_production_postgres_data":
        raise ValueError("Production database volume must remain isolated")
    host = services["web"]["environment"]["SITE_HOST"]
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
        raise ValueError("SITE_HOST must be a lowercase hostname without scheme, path or port")
    https_port = next(p["published"] for p in services["web"]["ports"] if p["target"] == 443)
    expected = f"https://{host}" + (f":{https_port}" if str(https_port) != "443" else "")
    origins = json.loads(backend["AUTH_ALLOWED_ORIGINS"])
    if not isinstance(origins, list) or expected not in origins:
        raise ValueError("AUTH_ALLOWED_ORIGINS must include the exact HTTPS website origin")
    for origin in origins:
        if not isinstance(origin, str):
            raise ValueError("Production origins must be HTTPS strings")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "*" in origin
            or origin != f"https://{parsed.netloc}"
        ):
            raise ValueError("Production origins must be explicit HTTPS origins")


def check_certificate(config: dict) -> None:
    web = config["services"]["web"]
    host = web["environment"]["SITE_HOST"]
    mount = next(v for v in web["volumes"] if v["target"] == "/etc/nginx/tls")
    directory = Path(mount["source"])
    certificate, key = directory / "fullchain.pem", directory / "privkey.pem"
    if not certificate.is_file() or not key.is_file():
        raise ValueError("TLS directory must contain fullchain.pem and privkey.pem")
    if any(not p.resolve().is_relative_to(directory.resolve()) for p in (certificate, key)):
        raise ValueError("Certificate symlink targets must stay inside the mounted TLS directory")
    for args in (
        ["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "86400"],
        ["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", host],
    ):
        subprocess.run(args, check=True, capture_output=True, timeout=10)
    public_keys = []
    for args in (
        ["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"],
        ["openssl", "pkey", "-in", str(key), "-pubout"],
    ):
        public_keys.append(subprocess.run(args, check=True, capture_output=True, timeout=10).stdout)
    if public_keys[0] != public_keys[1]:
        raise ValueError("TLS certificate and private key do not match")


def execute(args: argparse.Namespace, env_file: Path) -> None:
    env = os.environ | {"DEPLOY_ENV_FILE": str(env_file)}
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "docker-compose.deploy.yml"),
    ]
    raw = subprocess.run(
        [*compose, "config", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    config = json.loads(raw.stdout)
    validate_config(config)
    check_certificate(config)
    print("Validated production project, image digests, origins, database and certificate")
    if args.check:
        return
    # Fetch images before changing running services. No build occurs on this host.
    subprocess.run([*compose, "pull", "web", "backend", "migrate"], env=env, check=True)
    subprocess.run([*compose, "up", "-d", "--wait", "db"], env=env, check=True)
    args.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = args.backup_dir / f"before-deploy-{stamp}.dump"
    db = config["services"]["db"]["environment"]
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "db",
                    "pg_dump",
                    "-Fc",
                    "-U",
                    db["POSTGRES_USER"],
                    "-d",
                    db["POSTGRES_DB"],
                ],
                env=env,
                stdout=output,
                check=True,
            )
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    print(f"Database backup saved: {backup}")
    # Compose gates API startup on successful migration, then gates HTTPS on readiness.
    subprocess.run([*compose, "up", "-d", "--wait"], env=env, check=True)
    subprocess.run([*compose, "exec", "-T", "web", "nginx", "-t"], env=env, check=True)
    print("Production containers are ready. Verify the public HTTPS URL and retain the backup.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.production")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backups")
    parser.add_argument("--check", action="store_true", help="Validate without deploying")
    args = parser.parse_args()
    with ExitStack() as resources:
        if not args.check:
            lock = resources.enter_context((ROOT / ".deploy.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another production deployment is running") from error
        # All Compose commands use one private snapshot even if the operator edits the profile.
        profile = resources.enter_context(tempfile.NamedTemporaryFile(suffix=".env"))
        profile.write(args.env_file.read_bytes())
        profile.flush()
        execute(args, Path(profile.name))


if __name__ == "__main__":
    main()
