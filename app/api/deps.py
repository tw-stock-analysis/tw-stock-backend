from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.market import MarketService

DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


def get_market_service(session: DatabaseSession) -> MarketService:
    return MarketService(session)


MarketServiceDependency = Annotated[MarketService, Depends(get_market_service)]
