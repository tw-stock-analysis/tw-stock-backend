from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import ResearchServiceDependency
from app.schemas.research import (
    ComparisonQuery,
    ComparisonResponse,
    OverviewResponse,
    ScreenerQuery,
    ScreenerResponse,
)
from app.schemas.twse import EmptyQuery

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/screener", response_model=ScreenerResponse, summary="依本次日成交快照篩選上市證券")
async def screener(query: Annotated[ScreenerQuery, Query()], service: ResearchServiceDependency):
    return await service.screen(query)


@router.get(
    "/screener/export",
    response_class=Response,
    summary="重新取得並匯出完整篩選結果 CSV",
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
async def export(query: Annotated[ScreenerQuery, Query()], service: ResearchServiceDependency):
    return Response(
        await service.export(query),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="twse-screener.csv"'},
    )


@router.get(
    "/compare",
    response_model=ComparisonResponse,
    summary="依序取得成交與估值快照比較 2 至 4 檔證券",
)
async def compare(query: Annotated[ComparisonQuery, Query()], service: ResearchServiceDependency):
    return await service.compare(query)


@router.get(
    "/overview", response_model=OverviewResponse, summary="本次快照最新交易日的漲跌及成交量排行"
)
async def overview(query: Annotated[EmptyQuery, Query()], service: ResearchServiceDependency):
    return await service.overview()
