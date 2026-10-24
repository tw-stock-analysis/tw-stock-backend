"""Bounded, on-demand FinMind research contracts."""

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from app.schemas.common import APIModel
from app.schemas.stocks import StockSource


def taipei_today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


class AnalysisQuery(APIModel):
    start_date: date | None = None
    end_date: date = Field(default_factory=taipei_today)

    @model_validator(mode="after")
    def validate_range(self) -> "AnalysisQuery":
        if self.start_date is None:
            self.start_date = self.end_date - timedelta(days=180)
        if (
            self.start_date < date(1994, 10, 1)
            or self.start_date > self.end_date
            or self.end_date > taipei_today()
            or (self.end_date - self.start_date).days > 366
        ):
            raise ValueError("Use a past date range of at most 366 days since 1994-10-01")
        return self


class FinMindMeta(APIModel):
    source: Literal["FinMind"] = "FinMind"
    dataset: str
    source_url: str = "https://api.finmindtrade.com/api/v4/data"
    retrieved_at: datetime
    query_start: date
    query_end: date
    row_count: int
    retrieval: Literal["on_request"] = "on_request"
    price_basis: Literal["unadjusted", "source_adjusted"] | None = None
    units: dict[str, str]


class AnalysisError(APIModel):
    code: str
    message: str


class AnalysisSection[T](APIModel):
    status: Literal["ok", "error", "not_requested"]
    data: list[T] | None = None
    meta: FinMindMeta | None = None
    error: AnalysisError | None = None


class TechnicalIndicators(APIModel):
    ma20: str | None = None
    ma60: str | None = None
    rsi14: str | None = None
    macd: str | None = None
    macd_signal: str | None = None
    macd_histogram: str | None = None
    bollinger_middle: str | None = None
    bollinger_upper: str | None = None
    bollinger_lower: str | None = None


class HistoryPoint(TechnicalIndicators):
    date: date
    open: str
    high: str
    low: str
    close: str
    volume: str
    amount: str
    opening_reference: bool = False
    price_available: bool = True


class RevenuePoint(APIModel):
    month: str
    revenue: str
    yoy: str | None
    mom: str | None
    record_date: date
    recorded_at: date | None
    ytd_revenue: str | None = None
    ytd_yoy: str | None = None
    trailing12_revenue: str | None = None
    trailing12_yoy: str | None = None


class InstitutionTrade(APIModel):
    name: str
    label: str
    buy: str
    sell: str
    net: str
    net5: str | None
    net20: str | None


class InstitutionPoint(APIModel):
    date: date
    trades: list[InstitutionTrade]


class StockAnalysisResponse(APIModel):
    code: str
    name: str
    market: Literal["TWSE"] = "TWSE"
    start_date: date
    end_date: date
    revenue_start_month: str
    complete: bool
    identity_source: StockSource
    history: AnalysisSection[HistoryPoint]
    revenue: AnalysisSection[RevenuePoint]
    institutions: AnalysisSection[InstitutionPoint]
