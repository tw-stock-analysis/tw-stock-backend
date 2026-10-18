from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import FugleServiceDependency
from app.schemas.common import APIModel
from app.schemas.realtime import (
    CandlesQuery,
    HistoricalQuery,
    IntradayQuery,
    RealtimeResponse,
    TickerListQuery,
    TradesQuery,
)

router = APIRouter(prefix="/realtime", tags=["realtime"])
Symbol = Annotated[str, Path(min_length=2, max_length=12, pattern=r"^[A-Z0-9]+$")]
Input = Annotated[IntradayQuery, Query()]


def intraday_params(query: APIModel) -> dict:
    return {
        key: str(value).lower() if isinstance(value, bool) else value
        for key, value in query.model_dump(by_alias=True, exclude_none=True).items()
    }


@router.get("/tickers", response_model=RealtimeResponse)
async def tickers(
    query: Annotated[TickerListQuery, Query()],
    service: FugleServiceDependency,
) -> RealtimeResponse:
    return await service.fetch("tickers", params=intraday_params(query))


@router.get("/{symbol}/quote", response_model=RealtimeResponse)
async def quote(symbol: Symbol, query: Input, service: FugleServiceDependency) -> RealtimeResponse:
    return await service.fetch("quote", symbol, intraday_params(query))


@router.get("/{symbol}/ticker", response_model=RealtimeResponse)
async def ticker(symbol: Symbol, service: FugleServiceDependency) -> RealtimeResponse:
    return await service.fetch("ticker", symbol)


@router.get("/{symbol}/trades", response_model=RealtimeResponse)
async def trades(
    symbol: Symbol,
    query: Annotated[TradesQuery, Query()],
    service: FugleServiceDependency,
) -> RealtimeResponse:
    return await service.fetch("trades", symbol, intraday_params(query))


@router.get("/{symbol}/candles", response_model=RealtimeResponse)
async def candles(
    symbol: Symbol, query: Annotated[CandlesQuery, Query()], service: FugleServiceDependency
) -> RealtimeResponse:
    return await service.fetch("candles", symbol, intraday_params(query))


@router.get("/{symbol}/volumes", response_model=RealtimeResponse)
async def volumes(
    symbol: Symbol, query: Input, service: FugleServiceDependency
) -> RealtimeResponse:
    return await service.fetch("volumes", symbol, intraday_params(query))


@router.get("/{symbol}/history", response_model=RealtimeResponse)
async def history(
    symbol: Symbol,
    query: Annotated[HistoricalQuery, Query()],
    service: FugleServiceDependency,
) -> RealtimeResponse:
    return await service.fetch(
        "history",
        symbol,
        {
            "from": query.date_from.isoformat(),
            "to": query.date_to.isoformat(),
            "timeframe": query.timeframe,
            "adjusted": str(query.adjusted).lower(),
        },
    )


@router.get("/{symbol}/stats", response_model=RealtimeResponse)
async def stats(symbol: Symbol, service: FugleServiceDependency) -> RealtimeResponse:
    return await service.fetch("stats", symbol)
