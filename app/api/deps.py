from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.advanced_research import AdvancedResearchService
from app.services.finmind import FinMindService
from app.services.notices import NoticeService
from app.services.research import ResearchService
from app.services.stock_analysis import StockAnalysisService
from app.services.stocks import StockService
from app.services.twse import TwseService

DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


def get_twse_service(request: Request) -> TwseService:
    return request.app.state.twse_service


TwseServiceDependency = Annotated[TwseService, Depends(get_twse_service)]


def get_stock_service(twse: TwseServiceDependency) -> StockService:
    return StockService(twse)


StockServiceDependency = Annotated[StockService, Depends(get_stock_service)]


def get_research_service(twse: TwseServiceDependency) -> ResearchService:
    return ResearchService(twse)


ResearchServiceDependency = Annotated[ResearchService, Depends(get_research_service)]


def get_notice_service(twse: TwseServiceDependency) -> NoticeService:
    return NoticeService(twse)


NoticeServiceDependency = Annotated[NoticeService, Depends(get_notice_service)]


def get_finmind_service(request: Request) -> FinMindService:
    return request.app.state.finmind_service


FinMindServiceDependency = Annotated[FinMindService, Depends(get_finmind_service)]


def get_stock_analysis_service(
    stocks: StockServiceDependency, finmind: FinMindServiceDependency
) -> StockAnalysisService:
    return StockAnalysisService(stocks, finmind)


StockAnalysisServiceDependency = Annotated[
    StockAnalysisService, Depends(get_stock_analysis_service)
]


def get_advanced_research_service(
    stocks: StockServiceDependency, finmind: FinMindServiceDependency
) -> AdvancedResearchService:
    return AdvancedResearchService(stocks, finmind)


AdvancedResearchServiceDependency = Annotated[
    AdvancedResearchService, Depends(get_advanced_research_service)
]
