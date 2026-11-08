from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import APIModel
from app.schemas.instruments import Code


class WatchlistName(APIModel):
    name: str = Field(min_length=1, max_length=100)


class WatchlistAdd(APIModel):
    code: Code


class WatchlistOrder(APIModel):
    codes: list[Code] = Field(max_length=100)


class WatchlistStockResponse(APIModel):
    market: str
    stock_code: str
    position: int


class WatchlistResponse(APIModel):
    id: UUID
    name: str
    updated_at: datetime
    items: list[WatchlistStockResponse]


class WatchlistsResponse(APIModel):
    data: list[WatchlistResponse]
