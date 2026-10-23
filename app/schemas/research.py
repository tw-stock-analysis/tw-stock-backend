"""Public research views computed from fresh TWSE snapshots."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.common import APIModel, Pagination
from app.schemas.stocks import StockQuote, StockSource, StockValuation

Price = Annotated[Decimal, Field(ge=0, le=10**12, max_digits=20, decimal_places=6)]
Percent = Annotated[Decimal, Field(ge=-(10**6), le=10**6, max_digits=14, decimal_places=6)]


class ScreenerQuery(APIModel):
    q: str = Field(default="", max_length=100)
    min_price: Price | None = None
    max_price: Price | None = None
    min_change: Percent | None = None
    max_change: Percent | None = None
    min_volume: int | None = Field(default=None, ge=0, le=10**30)
    sort: Literal["code", "closing_price", "change_percent", "trade_volume", "trade_value"] = "code"
    order: Literal["asc", "desc"] = "asc"
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def ordered_ranges(self):
        for low, high in ((self.min_price, self.max_price), (self.min_change, self.max_change)):
            if low is not None and high is not None and low > high:
                raise ValueError("區間下限不可大於上限。")
        return self


class ResearchQuote(StockQuote):
    change_percent: str | None = None


class ScreenerResponse(APIModel):
    data: list[ResearchQuote]
    pagination: Pagination
    meta: StockSource
    calculation: str = "漲跌幅 = 漲跌價差 ÷（收盤價 − 漲跌價差）× 100，四捨五入至小數 4 位。"


class ComparisonQuery(APIModel):
    codes: str = Field(min_length=9, max_length=27, pattern=r"^[0-9A-Z]{4,6}(,[0-9A-Z]{4,6}){1,3}$")

    @model_validator(mode="after")
    def distinct_codes(self):
        codes = self.codes.split(",")
        if len(set(codes)) != len(codes):
            raise ValueError("比較代碼不可重複。")
        return self


class ComparisonItem(APIModel):
    code: str
    quote: ResearchQuote | None
    valuation: StockValuation | None


class ComparisonResponse(APIModel):
    data: list[ComparisonItem]
    quote_meta: StockSource
    valuation_meta: StockSource | None
    valuation_error: str | None = None


class OverviewResponse(APIModel):
    total: int
    rising: int
    declining: int
    unchanged: int
    unclassified: int
    excluded: int
    gainers: list[ResearchQuote]
    losers: list[ResearchQuote]
    volume_leaders: list[ResearchQuote]
    meta: StockSource
