from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import RequestSourceDependency
from app.schemas.advanced_research import AdvancedQuery, AdvancedResponse
from app.schemas.analysis import (
    AnalysisError,
    AnalysisQuery,
    AnalysisSection,
    StockAnalysisResponse,
)
from app.schemas.instruments import (
    Code,
    InstrumentRange,
    InstrumentRef,
    InstrumentSearch,
    InstrumentSearchResponse,
    Market,
)
from app.services.advanced_research import AdvancedResearchService
from app.services.errors import ServiceError
from app.services.instruments import InstrumentIdentityAdapter, InstrumentService
from app.services.research_transforms import price_rows
from app.services.stock_analysis import StockAnalysisService

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("", response_model=InstrumentSearchResponse)
async def instruments(query: Annotated[InstrumentSearch, Query()], source: RequestSourceDependency):
    return await InstrumentService(source).search(query)


@router.get("/{market}/{code}")
async def instrument(
    market: Market,
    code: Code,
    query: Annotated[InstrumentRange, Query()],
    source: RequestSourceDependency,
):
    service = InstrumentService(source)
    catalogue = await service.catalogue()
    ref = InstrumentRef(market=market, code=code)
    item = next(
        (
            r
            for r in catalogue.data
            if r.code == code and any(h.market == market for h in r.records)
        ),
        None,
    )
    if item is None:
        raise ServiceError("INSTRUMENT_NOT_FOUND", "來源參考名錄找不到此證券。", 404)
    assert query.start_date is not None
    prices: AnalysisSection[dict[str, str | None]] = AnalysisSection(status="not_requested")
    try:
        service.resolve(catalogue, ref, query.end_date)
        rows, meta = await source.fetch("TaiwanStockPrice", code, query.start_date, query.end_date)
        prices.data = price_rows(rows, code, query.start_date, query.end_date)
        prices.status, prices.meta = "ok", meta
    except ServiceError as error:
        prices.status = "error"
        prices.error = AnalysisError(code=error.code, message=error.message)
    return {
        "instrument": item,
        "prices": prices,
        "sources": catalogue.sources,
        "as_of": query.end_date,
        "market_at_as_of": None,
        "note": "指定日期控制價格區間；來源名錄不是完整的當日市場別歷史。",
    }


@router.get("/{market}/{code}/analysis", response_model=StockAnalysisResponse)
async def analysis(
    market: Market,
    code: Code,
    query: Annotated[AnalysisQuery, Query()],
    request: Request,
    source: RequestSourceDependency,
):
    identity = InstrumentIdentityAdapter(
        InstrumentService(source), InstrumentRef(market=market, code=code), query.end_date
    )
    result = await StockAnalysisService(identity, source).analyse(
        code, query, request.is_disconnected
    )
    result.market = market
    return result


@router.get("/{market}/{code}/research", response_model=AdvancedResponse)
async def advanced(
    market: Market,
    code: Code,
    query: Annotated[AdvancedQuery, Query()],
    request: Request,
    source: RequestSourceDependency,
):
    identity = InstrumentIdentityAdapter(
        InstrumentService(source), InstrumentRef(market=market, code=code), query.end_date
    )
    result = await AdvancedResearchService(identity, source).research(
        code, query, request.is_disconnected
    )
    result.market = market
    return result
