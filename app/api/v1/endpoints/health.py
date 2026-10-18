from fastapi import APIRouter

from app.api.deps import DatabaseSession
from app.schemas.common import ErrorResponse
from app.schemas.health import HealthResponse
from app.services.health import liveness, readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return liveness()


@router.get("/ready", response_model=HealthResponse, responses={503: {"model": ErrorResponse}})
async def ready(session: DatabaseSession) -> HealthResponse:
    return await readiness(session)
