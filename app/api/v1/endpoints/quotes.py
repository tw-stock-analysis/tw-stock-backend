from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import MarketServiceDependency
from app.schemas.market import QuotesQuery, QuotesResponse

router = APIRouter(prefix="/quotes", tags=["quotes"])


@router.get("", response_model=QuotesResponse)
async def quotes(
    query: Annotated[QuotesQuery, Query()],
    service: MarketServiceDependency,
) -> QuotesResponse:
    return await service.quotes(query.ids)
