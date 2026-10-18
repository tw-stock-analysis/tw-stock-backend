"""Stable envelopes for official datasets; source columns keep their native units."""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import APIModel, Pagination


class DataProvider(StrEnum):
    TWSE = "twse"
    TPEX = "tpex"
    TDCC = "tdcc"
    TAIFEX = "taifex"


class DataCategory(StrEnum):
    COMPANIES = "companies"
    DAILY_PRICES = "daily-prices"
    VALUATIONS = "valuations"
    MONTHLY_REVENUE = "monthly-revenue"
    INCOME_STATEMENTS = "income-statements"
    BALANCE_SHEETS = "balance-sheets"
    DIVIDENDS = "dividends"
    CORPORATE_ACTIONS = "corporate-actions"
    ANNOUNCEMENTS = "announcements"
    INSTITUTIONAL_TRADES = "institutional-trades"
    FOREIGN_HOLDINGS = "foreign-holdings"
    MARGIN_BALANCES = "margin-balances"
    LENDING_LIMITS = "lending-limits"
    HOLDING_DISTRIBUTIONS = "holding-distributions"
    INSIDER_HOLDINGS = "insider-holdings"
    INSIDER_PLEDGES = "insider-pledges"
    INSIDER_TRANSFERS = "insider-transfers"
    DAY_TRADING = "day-trading"
    AFTER_HOURS = "after-hours"
    CALENDAR = "calendar"
    FUTURES_PRICES = "futures-prices"
    OPTIONS_PRICES = "options-prices"
    FUTURES_INSTITUTIONAL = "futures-institutional"
    OPTIONS_INSTITUTIONAL = "options-institutional"
    FUTURES_LARGE_TRADERS = "futures-large-traders"
    OPTIONS_LARGE_TRADERS = "options-large-traders"
    PUT_CALL_RATIO = "put-call-ratio"
    ODD_LOT = "odd-lot"
    BLOCK_TRADING = "block-trading"


class RecordQuery(APIModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=32, pattern=r"^[\w.-]+$")
    date_from: date | None = None
    date_to: date | None = None
    page: int = Field(default=1, ge=1, le=100000)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def date_range(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


class DatasetQuery(RecordQuery):
    dataset: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")


class DatasetMeta(APIModel):
    dataset: str
    provider: DataProvider
    category: DataCategory
    market: str
    frequency: str
    source_url: str
    documentation_url: str
    license_url: str
    attribution: str
    usage: str
    # These are snapshot dates, not a claim about live prices or publication time.
    data_as_of: date | None = None
    retrieved_at: datetime | None = None
    data_version: str | None = None
    freshness: Literal["unknown"] = "unknown"
    units: str = "source_native"
    coverage: str = "latest_source_snapshot"


class DatasetInfo(APIModel):
    meta: DatasetMeta
    status: Literal["available", "not_loaded"]
    record_count: int = 0
    industry: str | None = None
    symbol_filter_supported: bool | None = None


class DatasetCatalogResponse(APIModel):
    data: list[DatasetInfo]


class SourceRecord(APIModel):
    symbol: str | None
    record_date: date | None
    record_date_field: str | None = None
    # Financial tables have different industry taxonomies. Preserve exact source
    # names/strings instead of inventing common units or silently coercing EPS.
    fields: dict[str, str | None]


class DatasetRecordsResponse(APIModel):
    data: list[SourceRecord]
    pagination: Pagination
    meta: DatasetMeta
