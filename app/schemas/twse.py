from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import APIModel

Category = Literal[
    "prices",
    "indices",
    "foreign",
    "margin",
    "day-trading",
    "block",
    "alerts",
    "calendar",
    "warrants",
    "etf",
    "market",
]
View = Literal["prices", "series", "ranking", "holdings", "balances", "events", "calendar", "table"]


class EmptyQuery(APIModel):
    """TWSE's published snapshot APIs do not accept our own date/code parameters."""


class TradingDataset(APIModel):
    id: str
    title: str
    category: Category
    view: View
    path: str
    description: str
    search_hint: str
    source_url: str
    endpoint: str
    page_path: str


class TradingCatalogResponse(APIModel):
    data: list[TradingDataset]
    reference_url: str = "https://openapi.twse.com.tw/"
    scope: str = "證券交易、指數、注意／處置、權證交易及 ETF 交易統計；排除基本資料與財報。"


class RetrievalMeta(APIModel):
    source: Literal["TWSE"] = "TWSE"
    source_url: str
    retrieved_at: datetime
    retrieval: Literal["on_request"] = "on_request"
    freshness: Literal["source_defined"] = "source_defined"
    units: Literal["source_native"] = "source_native"
    row_count: int
    note: str = "每次請求直接取得官方最新發布資料；取得時間不代表成交時間或資料日期。"


class TradingResponse(APIModel):
    dataset: TradingDataset
    columns: list[str]
    data: list[dict[str, str | None]]
    meta: RetrievalMeta


class CatalogFilter(APIModel):
    category: Category | None = None
    q: str | None = Field(default=None, max_length=100)
