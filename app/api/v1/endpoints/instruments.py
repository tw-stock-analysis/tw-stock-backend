from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import MarketServiceDependency
from app.schemas.market import InstrumentListResponse, InstrumentQuery, OverviewResponse

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("", response_model=InstrumentListResponse)
async def list_instruments(
    query: Annotated[InstrumentQuery, Query()],
    service: MarketServiceDependency,
) -> InstrumentListResponse:
    return await service.list_instruments(query)


@router.get("/{instrument_id}/overview", response_model=OverviewResponse)
async def instrument_overview(
    instrument_id: UUID,
    service: MarketServiceDependency,
) -> OverviewResponse:
    return await service.overview(instrument_id)
