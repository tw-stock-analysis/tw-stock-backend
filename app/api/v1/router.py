from fastapi import APIRouter

from app.api.v1.endpoints import health, instruments, market, quotes
from app.schemas.common import ErrorResponse

router = APIRouter(
    prefix="/api/v1",
    responses={status: {"model": ErrorResponse} for status in (404, 422, 500, 503)},
)
router.include_router(health.router)
router.include_router(instruments.router)
router.include_router(quotes.router)
router.include_router(market.router)
