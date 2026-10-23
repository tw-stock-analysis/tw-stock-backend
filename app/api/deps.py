from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.notices import NoticeService
from app.services.research import ResearchService
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
