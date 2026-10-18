from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import APIModel, Pagination

Market = Literal["TWSE", "TPEX"]
DatasetKey = Literal["twse_instruments", "twse_daily_prices"]


class InstrumentQuery(APIModel):
    q: str = Field(default="", max_length=80)
    market: Market | None = None
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("q")
    @classmethod
    def trim_query(cls, value: str) -> str:
        return value.strip()


class QuotesQuery(APIModel):
    ids: list[UUID] = Field(min_length=1, max_length=100)

    @field_validator("ids")
    @classmethod
    def unique_ids(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class DataMeta(APIModel):
    source: Literal["TWSE"] = "TWSE"
    source_url: str
    attribution: str = "臺灣證券交易所"
    license_url: str = "https://data.gov.tw/license"
    data_as_of: date
    retrieved_at: datetime
    data_version: str
    # A verified exchange calendar is not available yet: never guess freshness.
    freshness: Literal["unknown"] = "unknown"


class InstrumentData(APIModel):
    id: UUID = Field(serialization_alias="instrumentId")
    ticker: str
    market: Market
    name: str
    company_name: str
    industry_code: str | None
    type: Literal["EQUITY"] = "EQUITY"


class InstrumentListResponse(APIModel):
    data: list[InstrumentData]
    pagination: Pagination
    meta: DataMeta


class DailyQuote(APIModel):
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume_shares: Decimal | None
    turnover: Decimal | None
    transactions: int | None
    change: Decimal | None
    change_basis: Literal["official_reference"] = "official_reference"
    trade_status: Literal["traded", "no_trade", "unknown"]
    price_basis: Literal["raw"] = "raw"
    currency: Literal["TWD"] = "TWD"
    missing_fields: list[str]
    missing_reasons: dict[str, Literal["SOURCE_MISSING", "NOT_COMPARABLE", "NO_TRADE"]]
    meta: DataMeta


class QuoteData(APIModel):
    instrument_id: UUID = Field(serialization_alias="instrumentId")
    status: Literal["available", "no_data", "not_found"]
    reason_code: str | None = None
    quote: DailyQuote | None = None


class QuotesResponse(APIModel):
    data: list[QuoteData]


class OverviewData(APIModel):
    instrument: InstrumentData
    instrument_meta: DataMeta
    quote: QuoteData


class OverviewResponse(APIModel):
    data: OverviewData


class DatasetStatus(APIModel):
    dataset: DatasetKey
    status: Literal["available", "not_loaded"]
    record_count: int = 0
    meta: DataMeta | None = None


class MarketStatusResponse(APIModel):
    data: list[DatasetStatus]
    supported_markets: list[Market] = ["TWSE"]
    price_scope: Literal["official_daily_summary"] = "official_daily_summary"
    realtime_supported: bool = False
