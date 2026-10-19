"""Migration regression tests use a fresh database, never downgrade the configured one."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.core import config as settings_module
from app.core.config import BACKEND_ROOT, get_settings
from app.models import AuthSession, User, VerificationToken, Watchlist, WatchlistItem
from app.services.accounts import AccountService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_TESTS") != "1" or os.getenv("RUN_MIGRATION_TESTS") != "1",
        reason="Set RUN_DB_TESTS=1 RUN_MIGRATION_TESTS=1; requires CREATE DATABASE permission",
    ),
]


@pytest_asyncio.fixture
async def migration_db(monkeypatch):
    settings = get_settings()
    name = f"migration_{uuid4().hex}"
    admin = create_async_engine(settings.sqlalchemy_url(), isolation_level="AUTOCOMMIT")
    url = settings.sqlalchemy_url().set(database=name)
    scoped_settings = settings.model_copy(
        update={"database_url": url.render_as_string(hide_password=False)}
    )
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def migrate(target="head", direction="upgrade"):
        await asyncio.to_thread(getattr(command, direction), config, target)

    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        monkeypatch.setattr(settings_module, "get_settings", lambda: scoped_settings)
        await migrate("0003_watchlist_tables")
        yield SimpleNamespace(factory=factory, migrate=migrate, config=config)
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_migration_repairs_order_and_revokes_legacy_credentials(migration_db):
    now = datetime.now(UTC)
    user_id, deleted_id, list_id = uuid4(), uuid4(), uuid4()
    first_id, newest_id, expired_id, deleted_token_id = [uuid4() for _ in range(4)]
    async with migration_db.factory.begin() as session:
        session.add_all(
            [
                User(id=user_id, email="owner@example.test", password_hash="$argon2id$test"),
                User(
                    id=deleted_id,
                    email="deleted@example.test",
                    password_hash="$argon2id$test",
                    deleted_at=now,
                ),
            ]
        )
        await session.flush()
        session.add(Watchlist(id=list_id, user_id=user_id, name="ETF"))
        await session.flush()
        session.add_all(
            [
                WatchlistItem(watchlist_id=list_id, stock_code="0050", position=0),
                WatchlistItem(watchlist_id=list_id, stock_code="2330", position=0),
                WatchlistItem(watchlist_id=list_id, stock_code="00632R", position=9),
                AuthSession(
                    user_id=deleted_id, token_hash=b"s" * 32, expires_at=now + timedelta(days=1)
                ),
            ]
        )
        # Version 0003 has no revoked_at column, so use its actual column contract.
        for token_id, owner, purpose, age, expires in [
            (first_id, user_id, "reset_password", 3, now + timedelta(hours=1)),
            (newest_id, user_id, "reset_password", 2, now + timedelta(hours=1)),
            (expired_id, user_id, "verify_email", 4, now - timedelta(hours=1)),
            (deleted_token_id, deleted_id, "reset_password", 1, now + timedelta(hours=1)),
        ]:
            await session.execute(
                text(
                    "INSERT INTO app.verification_tokens "
                    "(id, user_id, purpose, token_hash, created_at, expires_at) "
                    "VALUES (:id, :user_id, :purpose, :hash, :created, :expires)"
                ),
                {
                    "id": token_id,
                    "user_id": owner,
                    "purpose": purpose,
                    "hash": token_id.bytes * 2,
                    "created": now - timedelta(hours=age),
                    "expires": expires,
                },
            )
    await migration_db.migrate()
    async with migration_db.factory() as session:
        items = list(await session.scalars(select(WatchlistItem).order_by(WatchlistItem.position)))
        assert [(item.stock_code, item.position) for item in items] == [
            ("0050", 0),
            ("2330", 1),
            ("00632R", 2),
        ]
        for token_id in (first_id, expired_id, deleted_token_id):
            assert (await session.get(VerificationToken, token_id)).revoked_at is not None
        assert (await session.get(VerificationToken, newest_id)).revoked_at is None
        assert await session.scalar(select(AuthSession.revoked_at)) is not None
    await migration_db.migrate("0003_watchlist_tables", "downgrade")
    async with migration_db.factory() as session:
        for token_id in (first_id, expired_id, deleted_token_id):
            assert (
                await session.scalar(
                    text("SELECT consumed_at FROM app.verification_tokens WHERE id = :id"),
                    {"id": token_id},
                )
                is not None
            )
    await migration_db.migrate()
    await asyncio.to_thread(command.check, migration_db.config)


async def test_duplicate_names_abort_migration_without_renaming_or_partial_ddl(migration_db):
    user_id = uuid4()
    async with migration_db.factory.begin() as session:
        session.add(User(id=user_id, email="owner@example.test", password_hash="$argon2id$test"))
        await session.flush()
        session.add_all(
            [
                Watchlist(user_id=user_id, name="ETF"),
                Watchlist(user_id=user_id, name="etf"),
            ]
        )
    with pytest.raises(DBAPIError, match="Resolve duplicate watchlist names"):
        await migration_db.migrate()
    async with migration_db.factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0003_watchlist_tables"
        )
        assert set(await session.scalars(select(Watchlist.name))) == {"ETF", "etf"}
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='app' AND table_name='verification_tokens' "
                    "AND column_name='revoked_at'"
                )
            )
            == 0
        )
    async with migration_db.factory.begin() as session:
        await session.execute(
            update(Watchlist).where(Watchlist.name == "etf").values(name="另一份")
        )
    await migration_db.migrate()


async def test_email_reuse_blocks_unsafe_downgrade_without_deleting_accounts(migration_db):
    await migration_db.migrate()
    accounts = AccountService(migration_db.factory)
    old_id = await accounts.create("reuse@example.test", password_hash="$argon2id$old")
    await accounts.soft_delete(old_id)
    new_id = await accounts.create("REUSE@example.test", password_hash="$argon2id$new")
    with pytest.raises(DBAPIError, match="Email reuse prevents downgrade"):
        await migration_db.migrate("0003_watchlist_tables", "downgrade")
    async with migration_db.factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0004_account_rules"
        )
        assert await session.get(User, old_id) is not None
        assert await session.get(User, new_id) is not None
    async with migration_db.factory.begin() as session:
        await session.execute(delete(User).where(User.id == new_id))
    await migration_db.migrate("0003_watchlist_tables", "downgrade")
    await migration_db.migrate()


@pytest.mark.parametrize("kind", ["lists", "items"])
async def test_over_capacity_legacy_data_is_never_silently_deleted(migration_db, kind):
    user_id = uuid4()
    async with migration_db.factory.begin() as session:
        session.add(User(id=user_id, email="owner@example.test", password_hash="$argon2id$test"))
        await session.flush()
        lists = [
            Watchlist(user_id=user_id, name=str(index))
            for index in range(11 if kind == "lists" else 1)
        ]
        session.add_all(lists)
        await session.flush()
        if kind == "items":
            session.add_all(
                [
                    WatchlistItem(
                        watchlist_id=lists[0].id, stock_code=str(1000 + index), position=index
                    )
                    for index in range(101)
                ]
            )
    with pytest.raises(DBAPIError, match="Resolve watchlist capacity violations"):
        await migration_db.migrate()
    async with migration_db.factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0003_watchlist_tables"
        )
        query = (
            "SELECT count(*) FROM app.watchlists"
            if kind == "lists"
            else ("SELECT count(*) FROM app.watchlist_items")
        )
        assert await session.scalar(text(query)) == (11 if kind == "lists" else 101)
