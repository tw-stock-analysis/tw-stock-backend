from fastapi import APIRouter

from app.api.v1.endpoints import (
    data_sources,
    derivatives,
    fundamentals,
    health,
    instruments,
    market,
    market_data,
    ownership,
    quotes,
    realtime,
)
from app.schemas.common import ErrorResponse

router = APIRouter(
    prefix="/api/v1",
    responses={status: {"model": ErrorResponse} for status in (404, 422, 500, 503)},
)
router.include_router(health.router)
router.include_router(instruments.router)
router.include_router(quotes.router)
router.include_router(market.router)
router.include_router(data_sources.router)
router.include_router(fundamentals.router)
router.include_router(ownership.router)
router.include_router(market_data.router)
router.include_router(derivatives.router)
router.include_router(realtime.router)
