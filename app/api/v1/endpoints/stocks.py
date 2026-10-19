from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import StockServiceDependency
from app.schemas.stocks import (
    StockCode,
    StockMarginResponse,
    StockQuoteResponse,
    StockSearchQuery,
    StockSearchResponse,
    StockValuationResponse,
)
from app.schemas.twse import EmptyQuery

router = APIRouter(prefix="/stocks", tags=["stocks"])


@router.get("", response_model=StockSearchResponse, summary="依代碼或名稱搜尋本次上市證券")
async def search_stocks(
    query: Annotated[StockSearchQuery, Query()], service: StockServiceDependency
) -> StockSearchResponse:
    return await service.search(query)


@router.get("/{code}", response_model=StockQuoteResponse, summary="個股日成交摘要")
async def stock_quote(
    code: StockCode, query: Annotated[EmptyQuery, Query()], service: StockServiceDependency
) -> StockQuoteResponse:
    return await service.quote(code)


@router.get("/{code}/valuation", response_model=StockValuationResponse, summary="個股交易估值")
async def stock_valuation(
    code: StockCode, query: Annotated[EmptyQuery, Query()], service: StockServiceDependency
) -> StockValuationResponse:
    return await service.valuation(code)


@router.get("/{code}/margin", response_model=StockMarginResponse, summary="個股融資融券明細")
async def stock_margin(
    code: StockCode, query: Annotated[EmptyQuery, Query()], service: StockServiceDependency
) -> StockMarginResponse:
    return await service.margin(code)
