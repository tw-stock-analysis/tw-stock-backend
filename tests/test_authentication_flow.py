"""Real Argon2, HTTP cookie jars, and PostgreSQL through the application lifespan."""

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.passwords import PasswordService
from app.main import create_app
from app.models import AuthSession
from app.services.accounts import AccountService
from app.services.credentials import VerificationService, digest_token

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires migrated PostgreSQL"),
]

ORIGIN = "https://auth.example.test"
HEADERS = {"Origin": ORIGIN, "X-CSRF-Protection": "1", "Content-Type": "application/json"}
PASSWORD = "This is an integration password 測試 "


async def test_real_browser_flow_verification_login_device_revocation_and_logout(service_db):
    config = get_settings().model_copy(
        update={"app_env": "production", "auth_allowed_origins": [ORIGIN]}
    )
    app = create_app(config)
    password_hash = await PasswordService().hash(PASSWORD)
    email = service_db.email()
    accounts = AccountService(service_db.factory)
    user_id = await accounts.create(email, password_hash=password_hash)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url=ORIGIN) as desktop,
            httpx.AsyncClient(transport=transport, base_url=ORIGIN) as phone,
            httpx.AsyncClient(transport=transport, base_url=ORIGIN) as outsider,
        ):
            body = {"email": email.upper(), "password": PASSWORD}
            assert (await desktop.get("/api/v1/me")).status_code == 401
            assert (await desktop.post("/api/v1/auth/login", json=body)).status_code == 403
            pending = await desktop.post("/api/v1/auth/login", json=body, headers=HEADERS)
            assert pending.status_code == 401
            token = await VerificationService(service_db.factory).issue(
                user_id, "verify_email", timedelta(minutes=30)
            )
            await accounts.verify_email(token.value)

            first = await desktop.post("/api/v1/auth/login", json=body, headers=HEADERS)
            second = await phone.post("/api/v1/auth/login", json=body, headers=HEADERS)
            assert first.status_code == second.status_code == 200
            first_token = desktop.cookies.get("__Host-session")
            second_token = phone.cookies.get("__Host-session")
            assert first_token and second_token and first_token != second_token
            assert first_token not in first.text and second_token not in second.text
            assert "secure" in first.headers["set-cookie"].lower()
            assert (await desktop.get("/api/v1/me")).json()["id"] == str(user_id)
            devices = (await desktop.get("/api/v1/me/sessions")).json()["sessions"]
            assert len(devices) == 2 and sum(item["is_current"] for item in devices) == 1

            other_user = await service_db.user(
                password_hash=password_hash, email_verified_at=datetime.now(UTC)
            )
            other_login = await outsider.post(
                "/api/v1/auth/login",
                json={"email": other_user.email, "password": PASSWORD},
                headers=HEADERS,
            )
            assert other_login.status_code == 200
            revoked = await desktop.post("/api/v1/me/sessions/revoke-others", headers=HEADERS)
            assert revoked.status_code == 204 and revoked.content == b""
            assert (await desktop.get("/api/v1/me")).status_code == 200
            assert (await phone.get("/api/v1/me")).status_code == 401
            assert phone.cookies.get("__Host-session") is None
            assert (await outsider.get("/api/v1/me")).json()["id"] == str(other_user.id)

            async with service_db.factory() as session:
                records = list(
                    await session.scalars(select(AuthSession).where(AuthSession.user_id == user_id))
                )
                assert {record.token_hash for record in records} == {
                    digest_token(first_token),
                    digest_token(second_token),
                }
                assert all(
                    record.expires_at - record.created_at == timedelta(days=7) for record in records
                )
                assert sum(record.revoked_at is not None for record in records) == 1

            logged_out = await desktop.post("/api/v1/auth/logout", headers=HEADERS)
            assert logged_out.status_code == 204 and logged_out.content == b""
            assert desktop.cookies.get("__Host-session") is None
            replay = await desktop.get(
                "/api/v1/me", headers={"Cookie": f"__Host-session={first_token}"}
            )
            assert replay.status_code == 401
            assert (await desktop.post("/api/v1/auth/logout", headers=HEADERS)).status_code == 204


async def test_expired_cookie_cannot_access_private_http_endpoint(service_db):
    app = create_app(get_settings().model_copy(update={"auth_allowed_origins": [ORIGIN]}))
    owner = await service_db.user(
        password_hash=await PasswordService().hash(PASSWORD),
        email_verified_at=datetime.now(UTC),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            response = await client.post(
                "/api/v1/auth/login",
                headers=HEADERS,
                json={"email": owner.email, "password": PASSWORD},
            )
            assert response.status_code == 200
            async with service_db.factory.begin() as session:
                now = datetime.now(UTC)
                await session.execute(
                    update(AuthSession)
                    .where(AuthSession.user_id == owner.id)
                    .values(
                        created_at=now - timedelta(days=8),
                        last_seen_at=now - timedelta(days=8),
                        expires_at=now - timedelta(days=1),
                    )
                )
            expired = await client.get("/api/v1/me")
            assert expired.status_code == 401
            assert expired.json()["error"]["code"] == "SESSION_INVALID"
            assert client.cookies.get("session") is None
