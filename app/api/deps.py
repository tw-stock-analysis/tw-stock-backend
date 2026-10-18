from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.fugle import FugleService
from app.services.market import MarketService
from app.services.open_data import OpenDataService

DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


def get_market_service(session: DatabaseSession) -> MarketService:
    return MarketService(session)


MarketServiceDependency = Annotated[MarketService, Depends(get_market_service)]


def get_open_data_service(session: DatabaseSession) -> OpenDataService:
    return OpenDataService(session)


OpenDataServiceDependency = Annotated[OpenDataService, Depends(get_open_data_service)]


def get_fugle_service(request: Request) -> FugleService:
    return request.app.state.fugle_service


FugleServiceDependency = Annotated[FugleService, Depends(get_fugle_service)]
