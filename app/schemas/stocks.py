"""Stock views derived only from the current TWSE snapshot, without persistence."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import Field

from app.schemas.common import APIModel, Pagination

StockCode = Annotated[str, Path(pattern=r"^[0-9A-Z]{4,6}$")]


class StockSearchQuery(APIModel):
    q: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=50)


class StockSource(APIModel):
    source: Literal["TWSE"] = "TWSE"
    source_url: str
    retrieved_at: datetime
    data_date: str | None = None
    retrieval: Literal["on_request"] = "on_request"
    units: Literal["source_native"] = "source_native"
    note: str = "數值及單位沿用官方；取得時間不代表資料日期。"


class StockQuote(APIModel):
    code: str
    name: str
    date: str | None = None
    opening_price: str | None = None
    highest_price: str | None = None
    lowest_price: str | None = None
    closing_price: str | None = None
    change: str | None = None
    trade_volume: str | None = None
    trade_value: str | None = None
    transactions: str | None = None


class StockSearchResponse(APIModel):
    data: list[StockQuote]
    pagination: Pagination
    meta: StockSource


class StockQuoteResponse(APIModel):
    data: StockQuote
    meta: StockSource


class StockValuation(APIModel):
    code: str
    name: str
    date: str | None = None
    pe_ratio: str | None = None
    dividend_yield: str | None = None
    pb_ratio: str | None = None


class StockValuationResponse(APIModel):
    data: StockValuation | None
    meta: StockSource


class StockMargin(APIModel):
    code: str
    name: str
    margin_buy: str | None = None
    margin_sell: str | None = None
    margin_repayment: str | None = None
    margin_previous_balance: str | None = None
    margin_balance: str | None = None
    margin_limit: str | None = None
    short_sell: str | None = None
    short_buy: str | None = None
    short_repayment: str | None = None
    short_previous_balance: str | None = None
    short_balance: str | None = None
    short_limit: str | None = None
    offsetting: str | None = None
    note: str | None = None


class StockMarginResponse(APIModel):
    data: StockMargin | None
    meta: StockSource
