import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from ipaddress import ip_address
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models import AuthSession, User, VerificationToken

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1 with migrated PostgreSQL"
    ),
]


def make_user(**overrides):
    # A placeholder encoded hash only: these tests do not implement password hashing.
    values = {"email": f"{uuid4().hex}@example.test", "password_hash": "$argon2id$test-only"}
    return User(**(values | overrides))


def make_session(user_id, **overrides):
    values = {
        "user_id": user_id,
        "token_hash": sha256(uuid4().bytes).digest(),
        "expires_at": datetime.now(UTC) + timedelta(days=1),
    }
    return AuthSession(**(values | overrides))


def make_verification(user_id, **overrides):
    values = {
        "user_id": user_id,
        "purpose": "verify_email",
        "token_hash": sha256(uuid4().bytes).digest(),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    return VerificationToken(**(values | overrides))


@pytest_asyncio.fixture
async def user(db_session):
    record = make_user()
    db_session.add(record)
    await db_session.flush()
    return record


async def test_real_asyncpg_database_and_namespaces():
    engine = create_async_engine(get_settings().sqlalchemy_url())
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            schemas = (
                (
                    await connection.execute(
                        text(
                            "SELECT schema_name FROM information_schema.schemata "
                            "WHERE schema_name IN ('app', 'market')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(schemas) == {"app", "market"}
            tables = (
                await connection.execute(
                    text(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_schema IN ('app', 'market')"
                    )
                )
            ).all()
            assert set(tables) == {
                ("app", "users"),
                ("app", "auth_sessions"),
                ("app", "verification_tokens"),
                ("app", "watchlists"),
                ("app", "watchlist_items"),
            }
    finally:
        await engine.dispose()


async def test_user_normalization_defaults_and_update_timestamp(db_session):
    old_time = datetime.now(UTC) - timedelta(days=1)
    user = make_user(email="  Mixed.Case+Alias@Example.TEST  ", updated_at=old_time)
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)

    assert user.id is not None
    assert user.email == "mixed.case+alias@example.test"
    assert user.status == "pending_verification"
    assert user.created_at.utcoffset() == timedelta(0)
    assert user.email_verified_at is None
    assert user.deleted_at is None

    user.display_name = "台股研究員"
    await db_session.flush()
    await db_session.refresh(user)
    assert user.updated_at > old_time


@pytest.mark.parametrize("soft_deleted", [False, True])
async def test_normalized_email_unique_even_after_soft_delete(db_session, user, soft_deleted):
    if soft_deleted:
        user.deleted_at = datetime.now(UTC)
        await db_session.flush()
    db_session.add(make_user(email=f"  {user.email.upper()}  "))
    with pytest.raises(IntegrityError, match="uq_users_email"):
        await db_session.flush()


@pytest.mark.parametrize("email", ["Mixed@Example.test", " spaced@example.test ", ""])
async def test_database_rejects_noncanonical_email_when_orm_is_bypassed(db_session, email):
    with pytest.raises(IntegrityError, match="ck_users_email_normalized"):
        await db_session.execute(
            insert(User.__table__).values(email=email, password_hash="$argon2id$test-only")
        )


@pytest.mark.parametrize("status", ["pending_verification", "active", "disabled"])
async def test_supported_user_statuses(db_session, status):
    user = make_user(status=status)
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    assert user.status == status


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"status": "administrator"}, "ck_users_status_allowed"),
        ({"password_hash": ""}, "ck_users_password_hash_not_empty"),
        ({"password_hash": None}, "not-null constraint"),
    ],
)
async def test_invalid_user_data(db_session, changes, constraint):
    db_session.add(make_user(**changes))
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()


async def test_email_length_limit(db_session):
    db_session.add(make_user(email="a" * 241 + "@example.test"))
    await db_session.flush()
    db_session.add(make_user(email="a" * 242 + "@example.test"))
    with pytest.raises(DBAPIError, match="value too long") as caught:
        await db_session.flush()
    assert caught.value.orig.sqlstate == "22001"


async def test_multiple_sessions_and_revoking_other_devices(db_session, user):
    current = make_session(user.id, user_agent="Desktop")
    other = make_session(user.id, user_agent="Mobile")
    db_session.add_all([current, other])
    await db_session.flush()

    await db_session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.id != current.id)
        .values(revoked_at=func.now())
    )
    await db_session.refresh(current)
    await db_session.refresh(other)
    assert current.revoked_at is None
    assert other.revoked_at is not None


@pytest.mark.parametrize("ip", ["192.0.2.10", "2001:db8::10"])
async def test_relationships_and_ipv4_ipv6_round_trip(db_session, user, ip):
    session = make_session(user.id, created_ip=ip_address(ip))
    verification = make_verification(user.id, created_ip=ip_address(ip))
    db_session.add_all([session, verification])
    await db_session.flush()
    db_session.expunge_all()

    loaded = await db_session.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.sessions), selectinload(User.verification_tokens))
    )
    assert loaded.sessions[0].created_ip == ip_address(ip)
    assert loaded.verification_tokens[0].created_ip == ip_address(ip)
    assert loaded.sessions[0].token_hash == session.token_hash


@pytest.mark.parametrize("purpose", ["verify_email", "reset_password"])
async def test_verification_purposes_and_consumption_timestamp(db_session, user, purpose):
    record = make_verification(user.id, purpose=purpose)
    db_session.add(record)
    await db_session.flush()
    assert record.consumed_at is None
    record.consumed_at = datetime.now(UTC)
    await db_session.flush()
    await db_session.refresh(record)
    assert record.purpose == purpose
    assert record.consumed_at >= record.created_at


@pytest.mark.parametrize("kind", ["session", "verification"])
@pytest.mark.parametrize("invalid", ["hash_length", "missing_hash", "expiry", "orphan", "time"])
async def test_session_and_verification_constraints(db_session, user, kind, invalid):
    now = datetime.now(UTC)
    factory = make_session if kind == "session" else make_verification
    record = factory(user.id, created_at=now)
    constraints = {
        "hash_length": "token_hash_sha256",
        "missing_hash": "not-null constraint",
        "expiry": "expiry_after_creation",
        "orphan": "user_id_users",
        "time": "revocation_after_creation" if kind == "session" else "consumption_after_creation",
    }
    if invalid == "hash_length":
        record.token_hash = b"plaintext-token"
    elif invalid == "missing_hash":
        record.token_hash = None
    elif invalid == "expiry":
        record.expires_at = now
    elif invalid == "orphan":
        record.user_id = uuid4()
    else:
        field = "revoked_at" if kind == "session" else "consumed_at"
        setattr(record, field, now - timedelta(seconds=1))
    if kind == "session":
        record.last_seen_at = now
    db_session.add(record)
    with pytest.raises(IntegrityError, match=constraints[invalid]):
        await db_session.flush()


async def test_session_last_seen_cannot_precede_creation(db_session, user):
    now = datetime.now(UTC)
    db_session.add(make_session(user.id, created_at=now, last_seen_at=now - timedelta(seconds=1)))
    with pytest.raises(IntegrityError, match="last_seen_after_creation"):
        await db_session.flush()


async def test_unknown_verification_purpose_rejected(db_session, user):
    db_session.add(make_verification(user.id, purpose="partner_login"))
    with pytest.raises(IntegrityError, match="purpose_allowed"):
        await db_session.flush()


@pytest.mark.parametrize("factory", [make_session, make_verification])
async def test_token_hash_unique_across_users(db_session, user, factory):
    other_user = make_user()
    db_session.add(other_user)
    await db_session.flush()
    digest = sha256(b"test-only-high-entropy-token-placeholder").digest()
    db_session.add(factory(user.id, token_hash=digest))
    await db_session.flush()
    db_session.add(factory(other_user.id, token_hash=digest))
    with pytest.raises(IntegrityError, match="token_hash"):
        await db_session.flush()


@pytest.mark.parametrize("load_relationships", [False, True])
async def test_hard_delete_cascades_private_records(db_session, user, load_relationships):
    db_session.add_all([make_session(user.id), make_verification(user.id)])
    await db_session.flush()
    db_session.expunge_all()
    query = select(User).where(User.id == user.id)
    if load_relationships:
        query = query.options(selectinload(User.sessions), selectinload(User.verification_tokens))
    loaded = await db_session.scalar(query)
    await db_session.delete(loaded)
    await db_session.flush()
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 0
    assert await db_session.scalar(select(func.count()).select_from(VerificationToken)) == 0
