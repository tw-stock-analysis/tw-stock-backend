import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.models import User


@pytest_asyncio.fixture
async def db_session():
    """Every test rolls back, including commits made by the ORM under test."""
    if os.getenv("RUN_DB_TESTS") != "1":
        pytest.skip("Set RUN_DB_TESTS=1 with migrated PostgreSQL")
    engine = create_async_engine(get_settings().sqlalchemy_url())
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@dataclass
class ServiceDatabase:
    factory: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
    prefix: str

    def email(self) -> str:
        return f"{self.prefix}{uuid4().hex}@example.test"

    async def user(self, **values) -> User:
        user = User(
            **(
                {
                    "email": self.email(),
                    "password_hash": "$argon2id$test-only",
                    "status": "active",
                }
                | values
            )
        )
        async with self.factory.begin() as session:
            session.add(user)
        return user


@pytest_asyncio.fixture
async def service_db():
    """Committed data and independent connections for actual lock/concurrency tests."""
    if os.getenv("RUN_DB_TESTS") != "1":
        pytest.skip("Set RUN_DB_TESTS=1 with migrated PostgreSQL")
    engine = create_async_engine(get_settings().sqlalchemy_url())
    database = ServiceDatabase(
        async_sessionmaker(engine, expire_on_commit=False), engine, f"integration-{uuid4().hex}-"
    )
    try:
        yield database
    finally:
        async with database.factory.begin() as session:
            await session.execute(delete(User).where(User.email.startswith(database.prefix)))
        await engine.dispose()
