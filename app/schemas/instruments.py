"""Fresh multi-market reference data; never claims a permanent historical security ID."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.analysis import FinMindMeta, taipei_today
from app.schemas.common import APIModel, Pagination

Market = Literal["TWSE", "TPEX", "EMERGING", "DELISTED"]
Code = Annotated[str, Field(pattern=r"^[0-9A-Z]{4,6}$")]


class InstrumentRef(APIModel):
    market: Market = "TWSE"
    code: Code


class ReferenceRecord(APIModel):
    name: str
    market: Market
    source_date: date | None
    kind: Literal["reference", "delisting"]


class Instrument(APIModel):
    market: Market
    code: str
    name: str
    industry: str | None = None
    reference_date: date | None
    delisted_on: date | None = None
    records: list[ReferenceRecord]
    historical_identity: Literal["source_limited", "ambiguous"] = "source_limited"
    note: str = "來源日期不是上市日；歷史列僅供身分參考，不保證當日市場別或永久證券身分。"


class InstrumentSearch(APIModel):
    market: Literal["ALL", "TWSE", "TPEX", "EMERGING", "DELISTED"] = "ALL"
    q: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=100)


class InstrumentSearchResponse(APIModel):
    data: list[Instrument]
    pagination: Pagination
    sources: list[FinMindMeta]


class InstrumentRange(APIModel):
    start_date: date | None = None
    end_date: date = Field(default_factory=taipei_today)

    @model_validator(mode="after")
    def valid_range(self) -> "InstrumentRange":
        if self.start_date is None:
            from datetime import timedelta

            self.start_date = self.end_date - timedelta(days=365)
        if (
            self.start_date < date(1994, 10, 1)
            or self.start_date > self.end_date
            or (self.end_date > taipei_today() or (self.end_date - self.start_date).days > 1830)
        ):
            raise ValueError("Use a past date range of at most 1830 days")
        return self
