"""Authentication against real PostgreSQL, including credential-change races."""

import asyncio
import os
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.models import AuthSession, User
from app.services.accounts import AccountService
from app.services.authentication import SESSION_TTL, AuthenticationService
from app.services.credentials import VerificationService, digest_token
from app.services.errors import ServiceError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires migrated PostgreSQL"),
]

PASSWORD = "保留 whitespace 密碼 "
ENCODED = "$argon2id$test-password"


class Passwords:
    """Deterministic verification lets tests stop precisely outside the DB lock."""

    def __init__(self, database, before_return=None):
        self.database = database
        self.before_return = before_return
        self.calls = []

    async def verify(self, password, encoded):
        assert self.database.engine.pool.checkedout() == 0
        self.calls.append((password, encoded))
        if self.before_return:
            await self.before_return()
        return password == PASSWORD and encoded == ENCODED


async def verified_user(database, **values):
    return await database.user(
        **(
            {
                "password_hash": ENCODED,
                "email_verified_at": datetime.now(UTC),
                "display_name": "測試使用者",
            }
            | values
        )
    )


def service(database, before_return=None):
    return AuthenticationService(database.factory, Passwords(database, before_return))


async def assert_no_sessions(database, user_id):
    async with database.factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user_id)
            )
        ) == 0


async def test_login_normalizes_email_and_stores_only_digest_for_fixed_seven_days(service_db):
    owner = await verified_user(service_db)
    auth = service(service_db)
    result = await auth.login(
        f" {owner.email.upper()} ",
        PASSWORD,
        user_agent="browser/device",
        created_ip=ip_address("2001:db8::1"),
    )
    assert result.user.id == owner.id
    assert result.user.display_name == "測試使用者"
    assert len(result.credential.value) == 43
    assert result.credential.value not in repr(result)
    assert "password_hash" not in {field.name for field in fields(result.user)}
    async with service_db.factory() as session:
        record = await session.get(AuthSession, result.credential.id)
        assert record.token_hash == digest_token(result.credential.value)
        assert record.expires_at - record.created_at == SESSION_TTL == timedelta(days=7)
        assert record.last_seen_at == record.created_at
        assert record.user_agent == "browser/device"
        assert record.created_ip == ip_address("2001:db8::1")


@pytest.mark.parametrize(
    "state", ["wrong_password", "pending", "disabled", "unverified", "deleted"]
)
async def test_login_failures_share_response_and_never_issue_session(service_db, state):
    values = {}
    if state in ("pending", "disabled"):
        values["status"] = "pending_verification" if state == "pending" else state
    if state == "unverified":
        values["email_verified_at"] = None
    if state == "deleted":
        values["deleted_at"] = datetime.now(UTC)
    owner = await verified_user(service_db, **values)
    passwords = Passwords(service_db)
    with pytest.raises(ServiceError) as caught:
        await AuthenticationService(service_db.factory, passwords).login(
            owner.email, "wrong" if state == "wrong_password" else PASSWORD
        )
    assert (caught.value.status_code, caught.value.code) == (401, "LOGIN_INVALID")
    assert len(passwords.calls) == 1
    await assert_no_sessions(service_db, owner.id)


async def test_unknown_account_still_calls_expensive_dummy_verification(service_db):
    passwords = Passwords(service_db)
    with pytest.raises(ServiceError) as caught:
        await AuthenticationService(service_db.factory, passwords).login(
            service_db.email(), PASSWORD
        )
    assert caught.value.code == "LOGIN_INVALID"
    assert passwords.calls == [(PASSWORD, None)]


async def test_password_reset_between_verification_and_issuance_rejects_old_password(service_db):
    owner = await verified_user(service_db)
    reset = await VerificationService(service_db.factory).issue(
        owner.id, "reset_password", timedelta(minutes=30)
    )

    async def reset_after_password_read():
        await AccountService(service_db.factory).reset_password(
            reset.value, password_hash="$argon2id$changed"
        )

    with pytest.raises(ServiceError) as caught:
        await service(service_db, reset_after_password_read).login(owner.email, PASSWORD)
    assert caught.value.code == "LOGIN_INVALID"
    await assert_no_sessions(service_db, owner.id)


async def test_delete_and_reregister_during_login_cannot_transfer_identity(service_db):
    owner = await verified_user(service_db)
    fresh_id = None

    async def replace_account():
        nonlocal fresh_id
        await AccountService(service_db.factory).soft_delete(owner.id)
        fresh = await verified_user(service_db, email=owner.email)
        fresh_id = fresh.id

    with pytest.raises(ServiceError) as caught:
        await service(service_db, replace_account).login(owner.email, PASSWORD)
    assert caught.value.code == "LOGIN_INVALID"
    assert fresh_id != owner.id
    await assert_no_sessions(service_db, owner.id)
    await assert_no_sessions(service_db, fresh_id)


async def test_authentication_updates_activity_without_extending_lifetime(service_db):
    owner = await verified_user(service_db)
    auth = service(service_db)
    issued = await auth.login(owner.email, PASSWORD)
    async with service_db.factory() as session:
        original = await session.get(AuthSession, issued.credential.id)
        created, last_seen, expiry = original.created_at, original.last_seen_at, original.expires_at
    current = await auth.authenticate(issued.credential.value)
    assert (current.id, current.user_id) == (issued.credential.id, owner.id)
    assert current.user.email == owner.email
    async with service_db.factory() as session:
        refreshed = await session.get(AuthSession, current.id)
        assert refreshed.last_seen_at > last_seen
        assert refreshed.created_at == created and refreshed.expires_at == expiry


@pytest.mark.parametrize(
    "state", ["expired", "revoked", "deleted", "disabled", "pending", "unverified"]
)
async def test_authentication_rechecks_session_and_account_state(service_db, state):
    owner = await verified_user(service_db)
    auth = service(service_db)
    issued = await auth.login(owner.email, PASSWORD)
    now = datetime.now(UTC)
    async with service_db.factory.begin() as session:
        if state in ("expired", "revoked"):
            values = (
                {
                    "created_at": now - timedelta(days=8),
                    "last_seen_at": now - timedelta(days=8),
                    "expires_at": now - timedelta(days=1),
                }
                if state == "expired"
                else {"revoked_at": now}
            )
            await session.execute(
                update(AuthSession).where(AuthSession.id == issued.credential.id).values(**values)
            )
        else:
            values = {
                "deleted": {"deleted_at": now},
                "disabled": {"status": "disabled"},
                "pending": {"status": "pending_verification"},
                "unverified": {"email_verified_at": None},
            }[state]
            await session.execute(update(User).where(User.id == owner.id).values(**values))
    with pytest.raises(ServiceError) as caught:
        await auth.authenticate(issued.credential.value)
    assert (caught.value.status_code, caught.value.code) == (401, "SESSION_INVALID")


async def test_verification_credential_is_never_a_login_session(service_db):
    owner = await verified_user(service_db)
    token = await VerificationService(service_db.factory).issue(
        owner.id, "reset_password", timedelta(minutes=30)
    )
    with pytest.raises(ServiceError) as caught:
        await service(service_db).authenticate(token.value)
    assert caught.value.code == "SESSION_INVALID"


@pytest.mark.parametrize("token", ["", "x" * 42, "x" * 44, "/" * 43, "非" * 43])
async def test_malformed_session_tokens_fail_before_acquiring_connection(service_db, token):
    class ForbiddenFactory:
        def begin(self):
            raise AssertionError("Malformed tokens must not query the DB")

    auth = AuthenticationService(ForbiddenFactory(), Passwords(service_db))
    with pytest.raises(ServiceError) as caught:
        await auth.authenticate(token)
    assert caught.value.code == "SESSION_INVALID"
    await auth.logout(token)
    await auth.logout(None)


async def test_device_listing_and_revoke_others_are_owner_scoped(service_db):
    owner, outsider = await verified_user(service_db), await verified_user(service_db)
    auth = service(service_db)
    first = await auth.login(owner.email, PASSWORD, user_agent="current")
    second = await auth.login(owner.email, PASSWORD, user_agent="another")
    outside = await auth.login(outsider.email, PASSWORD, user_agent="other user")
    current = await auth.authenticate(first.credential.value)
    devices = await auth.list_sessions(current)
    assert {row.id for row in devices} == {first.credential.id, second.credential.id}
    assert [(row.id, row.is_current) for row in devices if row.is_current] == [
        (first.credential.id, True)
    ]
    assert "token_hash" not in {field.name for field in fields(devices[0])}
    await auth.revoke_others(current)
    assert [row.id for row in await auth.list_sessions(current)] == [first.credential.id]
    await auth.authenticate(first.credential.value)
    await auth.authenticate(outside.credential.value)
    with pytest.raises(ServiceError):
        await auth.authenticate(second.credential.value)


async def test_revoked_dependency_result_cannot_revoke_newer_sessions(service_db):
    owner = await verified_user(service_db)
    auth = service(service_db)
    first = await auth.login(owner.email, PASSWORD)
    current = await auth.authenticate(first.credential.value)
    await auth.logout(first.credential.value)
    fresh = await auth.login(owner.email, PASSWORD)
    for operation in (auth.revoke_others, auth.list_sessions):
        with pytest.raises(ServiceError) as caught:
            await operation(current)
        assert caught.value.code == "SESSION_INVALID"
    await auth.authenticate(fresh.credential.value)


async def test_logout_is_idempotent_and_revokes_expired_sessions(service_db):
    owner = await verified_user(service_db)
    auth = service(service_db)
    issued = await auth.login(owner.email, PASSWORD)
    now = datetime.now(UTC)
    async with service_db.factory.begin() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == issued.credential.id)
            .values(
                created_at=now - timedelta(days=8),
                last_seen_at=now - timedelta(days=8),
                expires_at=now - timedelta(days=1),
            )
        )
    await auth.logout(issued.credential.value)
    await auth.logout(issued.credential.value)
    await auth.logout("A" * 43)
    async with service_db.factory() as session:
        assert (await session.get(AuthSession, issued.credential.id)).revoked_at is not None


async def test_expiry_is_checked_after_waiting_for_user_lock(service_db):
    owner = await verified_user(service_db)
    auth = service(service_db)
    issued = await auth.login(owner.email, PASSWORD)
    # A committed short expiry makes the test independent of transaction snapshots.
    async with service_db.factory.begin() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == issued.credential.id)
            .values(expires_at=datetime.now(UTC) + timedelta(milliseconds=300))
        )
    async with service_db.factory.begin() as blocker:
        await blocker.scalar(select(User).where(User.id == owner.id).with_for_update())
        pending = asyncio.create_task(auth.authenticate(issued.credential.value))
        await asyncio.sleep(0.5)
        assert not pending.done()
    with pytest.raises(ServiceError) as caught:
        await pending
    assert caught.value.code == "SESSION_INVALID"


async def test_failed_commit_never_returns_credential_and_rolls_back_insert(service_db):
    owner = await verified_user(service_db)

    class FailingSession(Session):
        pass

    def fail_commit(_session):
        raise RuntimeError("controlled commit failure")

    event.listen(FailingSession, "before_commit", fail_commit)

    class FailingAsyncSession(AsyncSession):
        sync_session_class = FailingSession

    factory = async_sessionmaker(
        service_db.engine, class_=FailingAsyncSession, expire_on_commit=False
    )
    auth = AuthenticationService(factory, Passwords(service_db))
    with pytest.raises(RuntimeError, match="controlled commit failure"):
        await auth.login(owner.email, PASSWORD)
    await assert_no_sessions(service_db, owner.id)
