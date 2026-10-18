import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1 with local PostgreSQL"
)
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
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema IN ('app', 'market')"
                    )
                )
                == 0
            )
    finally:
        await engine.dispose()
