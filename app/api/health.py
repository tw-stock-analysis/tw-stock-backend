import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter(prefix="/health", tags=["health"])
INITIAL_REVISION = "0001_create_schemas"


@router.get("/live")
async def live() -> dict:
    return {"data": {"status": "ok", "service": "tw-stock-analysis-api"}}


@router.get("/ready")
async def ready(request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    try:
        async with asyncio.timeout(3):
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != INITIAL_REVISION:
            return unavailable(request)
    except (SQLAlchemyError, OSError, TimeoutError):
        return unavailable(request)
    return {"data": {"status": "ready", "service": "tw-stock-analysis-api"}}


def unavailable(request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "DEPENDENCY_UNAVAILABLE",
                "message": "Database connection or migrations are not ready.",
                "field_errors": [],
                "request_id": request.state.request_id,
            }
        },
    )
