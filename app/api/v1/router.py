from fastapi import APIRouter

from app.api.v1.endpoints import health, twse
from app.schemas.common import ErrorResponse

router = APIRouter(
    prefix="/api/v1",
    responses={status: {"model": ErrorResponse} for status in (404, 422, 500, 502, 503)},
)
router.include_router(health.router)
router.include_router(twse.router)
