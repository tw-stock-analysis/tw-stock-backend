import json
from pathlib import Path

import pytest

from app.services.twse import parse_snapshot


@pytest.fixture
def source_rows():
    """Synthetic contract fixtures, never used as runtime fallback data."""
    root = Path(__file__).parent / "fixtures"
    return (
        json.loads((root / "twse_master.json").read_text()),
        json.loads((root / "twse_prices.json").read_text()),
    )


@pytest.fixture
def snapshot(source_rows):
    return parse_snapshot(*source_rows)


@pytest.fixture
async def db_session():
    import os

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.config import get_settings

    if os.getenv("RUN_DB_TESTS") != "1":
        pytest.skip("Set RUN_DB_TESTS=1 with migrated test PostgreSQL")
    engine = create_async_engine(get_settings().sqlalchemy_url())
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                yield session
            await transaction.rollback()
    finally:
        await engine.dispose()
