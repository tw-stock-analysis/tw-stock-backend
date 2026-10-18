from fastapi import APIRouter

from app.api.deps import MarketServiceDependency
from app.schemas.market import MarketStatusResponse

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/status", response_model=MarketStatusResponse)
async def market_status(service: MarketServiceDependency) -> MarketStatusResponse:
    return await service.status()
