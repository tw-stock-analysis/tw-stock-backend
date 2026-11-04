"""Explicit single-period, whole-source snapshots, separate from candidate screening."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.analysis import AnalysisError, FinMindMeta, taipei_today
from app.schemas.common import APIModel, Pagination
from app.services.stock_analysis import month_shift


class MarketRules(APIModel):
    min_revenue_yoy: Decimal | None = Field(default=None, ge=-100000, le=100000)
    min_gross_margin: Decimal | None = Field(default=None, ge=-100000, le=100000)
    min_operating_margin: Decimal | None = Field(default=None, ge=-100000, le=100000)
    max_debt_ratio: Decimal | None = Field(default=None, ge=0, le=1000)


class MarketScreenQuery(APIModel):
    markets: list[Literal["TWSE", "TPEX", "EMERGING"]] = Field(
        default=["TWSE", "TPEX"], min_length=1, max_length=3
    )
    revenue_month: date
    report_date: date
    rules: MarketRules = Field(default_factory=MarketRules)
    sort: Literal["code", "revenue_yoy", "gross_margin", "operating_margin", "debt_ratio"] = "code"
    descending: bool = False
    result_filter: Literal["all", "matched", "insufficient"] = "matched"
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def periods(self):
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("Duplicate markets")
        today = taipei_today()
        if (
            self.revenue_month.day != 1
            or self.revenue_month < date(2001, 1, 1)
            or month_shift(self.revenue_month, 1) > today
        ):
            raise ValueError("Use a past revenue month, represented by its first day")
        if (self.report_date.month, self.report_date.day) not in {
            (3, 31),
            (6, 30),
            (9, 30),
            (12, 31),
        } or not date(2001, 1, 1) <= self.report_date <= today:
            raise ValueError("Use a past quarter-end date")
        return self


class MarketScreenRow(APIModel):
    market: Literal["TWSE", "TPEX", "EMERGING"]
    code: str
    name: str
    status: Literal["ok", "insufficient", "error"]
    matches: bool | None
    metrics: dict[str, str | None]
    dates: dict[str, str]
    error: AnalysisError | None = None


class MarketCoverage(APIModel):
    universe: int = Field(ge=0)
    matched: int = Field(ge=0)
    not_matched: int = Field(ge=0)
    insufficient: int = Field(ge=0)
    errors: int = Field(ge=0)
    complete: bool


class MarketScreenResponse(APIModel):
    rows: list[MarketScreenRow]
    pagination: Pagination
    coverage: MarketCoverage
    sources: list[FinMindMeta]
    notes: list[str]
