from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import NoticeServiceDependency, ResearchServiceDependency
from app.schemas.notices import CalendarQuery, CalendarResponse, RiskQuery, RiskResponse
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


@router.get("/calendar", response_model=CalendarResponse, summary="除權息與開休市公告日期篩選")
async def calendar(query: Annotated[CalendarQuery, Query()], service: NoticeServiceDependency):
    return await service.calendar(query)


@router.get(
    "/calendar/export",
    response_class=Response,
    summary="重新取得完整日期事件並下載 ICS 日曆",
    responses={200: {"content": {"text/calendar": {"schema": {"type": "string"}}}}},
)
async def calendar_export(
    query: Annotated[CalendarQuery, Query()], service: NoticeServiceDependency
):
    content, skipped = await service.calendar_export(query)
    return Response(
        content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="twse-calendar.ics"',
            "X-Skipped-Undated": str(skipped),
        },
    )


@router.get("/risks", response_model=RiskResponse, summary="逐次彙整官方注意、處置與暫停交易公告")
async def risks(query: Annotated[RiskQuery, Query()], service: NoticeServiceDependency):
    return await service.risks(query)
