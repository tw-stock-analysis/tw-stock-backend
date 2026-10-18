import asyncio
from functools import lru_cache

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import BACKEND_ROOT
from app.schemas.health import HealthData, HealthResponse
from app.services.errors import ServiceError


@lru_cache
def migration_head() -> str:
    head = ScriptDirectory.from_config(Config(str(BACKEND_ROOT / "alembic.ini"))).get_current_head()
    if head is None:
        raise RuntimeError("Missing migration head")
    return head


def liveness() -> HealthResponse:
    return HealthResponse(data=HealthData(status="ok"))


async def readiness(session: AsyncSession) -> HealthResponse:
    try:
        async with asyncio.timeout(3):
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        if revision == migration_head():
            return HealthResponse(data=HealthData(status="ready"))
    except (SQLAlchemyError, OSError, TimeoutError):
        pass
    raise ServiceError(
        "DEPENDENCY_UNAVAILABLE",
        "Database connection or migrations are not ready.",
        503,
    )
