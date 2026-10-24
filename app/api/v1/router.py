from fastapi import APIRouter

from app.api.v1.endpoints import (
    authentication,
    health,
    instruments,
    lab,
    research,
    stocks,
    twse,
    workspace,
)
from app.schemas.common import ErrorResponse

router = APIRouter(
    prefix="/api/v1",
    responses={status: {"model": ErrorResponse} for status in (404, 422, 500, 502, 503)},
)
router.include_router(health.router)
router.include_router(twse.router)
router.include_router(stocks.router)
router.include_router(research.router)
router.include_router(authentication.router)
router.include_router(instruments.router)
router.include_router(workspace.router)
router.include_router(lab.router)
