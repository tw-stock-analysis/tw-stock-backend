import os
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.main import create_app
from app.models import User, VerificationToken
from app.services import registration
from app.services.accounts import AccountService
from app.services.credentials import digest_token

ORIGIN = "http://localhost:3007"
HEADERS = {"Origin": ORIGIN, "X-CSRF-Protection": "1"}
PASSWORD = "長密碼 test registration password"


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires migrated PostgreSQL")
async def test_register_verify_login_and_revoke(service_db, tmp_path, monkeypatch):
    monkeypatch.setattr(registration, "BACKEND_ROOT", tmp_path)
    settings = get_settings().model_copy(
        update={"app_env": "development", "auth_mail_outbox": True}
    )
    app = create_app(settings)
    email = f"{service_db.prefix}{uuid4().hex[:12]}@example.test"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client,
    ):

        async def post(path, body):
            return await client.post(f"/api/v1/auth/{path}", json=body, headers=HEADERS)

        assert (
            await client.post("/api/v1/auth/register", json={"email": email})
        ).status_code == 403
        assert (await post("register", {"email": "not an address"})).status_code == 422
        start = await post("register", {"email": email.upper()})
        assert start.status_code == 202 and not start.content
        assert (await post("register", {"email": email})).status_code == 429
        files = list((tmp_path / ".auth-mail").glob("*.eml"))
        assert len(files) == 1
        message = BytesParser(policy=policy.default).parsebytes(files[0].read_bytes())
        assert message["To"] == email
        code = message.get_body(preferencelist=("plain",)).get_content().splitlines()[2]
        assert code not in start.text
        async with service_db.factory() as session:
            user = await session.scalar(select(User).where(User.email == email))
            assert user.status == "pending_verification" and user.email_verified_at is None
            token = await session.scalar(
                select(VerificationToken).where(VerificationToken.user_id == user.id)
            )
            assert token.token_hash == digest_token(code)

        login = {"email": email, "password": PASSWORD}
        assert (await post("login", login)).status_code == 401
        verify = {"token": code, "password": PASSWORD, "display_name": "  研究者  "}
        assert (await post("verify-email", verify | {"token": "a" * 43})).status_code == 400
        assert (await post("verify-email", verify | {"password": "short"})).status_code == 422
        assert (await post("verify-email", verify)).status_code == 204
        assert (await post("verify-email", verify)).status_code == 400
        assert (await client.get("/api/v1/me")).status_code == 401
        logged_in = await post("login", login)
        assert logged_in.status_code == 200
        assert "httponly" in logged_in.headers["set-cookie"].lower()
        me = (await client.get("/api/v1/me")).json()
        assert me["email"] == email and me["display_name"] == "研究者"
        assert "password_hash" not in me and "token" not in me
        assert (
            await client.post(
                "/api/v1/auth/logout", headers=HEADERS | {"Content-Type": "application/json"}
            )
        ).status_code == 204
        assert (await client.get("/api/v1/me")).status_code == 401

    # Duplicate registrations cannot send a new code or replace the existing password.
    accounts = AccountService(service_db.factory)
    assert await accounts.registration_code(email) is None
    other_email = f"{service_db.prefix}{uuid4().hex[:12]}@example.test"
    other = await accounts.registration_code(other_email)
    assert other is not None
    async with service_db.factory.begin() as session:
        now = datetime.now(UTC)
        await session.execute(
            update(VerificationToken)
            .where(VerificationToken.id == other.id)
            .values(
                created_at=now - timedelta(minutes=31),
                expires_at=now - timedelta(minutes=1),
            )
        )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client,
    ):
        expired = await client.post(
            "/api/v1/auth/verify-email",
            headers=HEADERS,
            json={
                "token": other.value,
                "password": PASSWORD,
            },
        )
        assert expired.status_code == 400 and expired.json()["error"]["code"] == "TOKEN_INVALID"
