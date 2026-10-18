from datetime import date, datetime
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from app.schemas.common import APIModel


class IntradayQuery(APIModel):
    type: Literal["oddlot"] | None = None


class TickerListQuery(APIModel):
    type: Literal["EQUITY", "INDEX", "WARRANT", "ODDLOT"] = "EQUITY"
    exchange: Literal["TWSE", "TPEx"] | None = None
    market: Literal["TSE", "OTC", "ESB", "TIB"] | None = None
    industry: str | None = Field(default=None, pattern=r"^\d{2}$")
    is_normal: bool | None = Field(default=None, alias="isNormal")
    is_attention: bool | None = Field(default=None, alias="isAttention")
    is_disposition: bool | None = Field(default=None, alias="isDisposition")
    is_halted: bool | None = Field(default=None, alias="isHalted")


class CandlesQuery(IntradayQuery):
    timeframe: Literal["1", "3", "5", "10", "15", "30", "60"] = "1"
    sort: Literal["asc", "desc"] = "asc"


class TradesQuery(IntradayQuery):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=100, ge=1, le=500)
    sort: Literal["asc", "desc"] = "desc"
    is_trial: bool | None = Field(default=None, alias="isTrial")


class HistoricalQuery(APIModel):
    date_from: date
    date_to: date
    timeframe: Literal["1", "3", "5", "10", "15", "30", "60", "D", "W", "M"] = "D"
    adjusted: bool = False

    @model_validator(mode="after")
    def validate_range(self):
        try:
            anniversary = self.date_from.replace(year=self.date_from.year + 1)
        except ValueError:
            anniversary = self.date_from.replace(year=self.date_from.year + 1, day=28)
        if not self.date_from <= self.date_to < anniversary:
            raise ValueError("Historical range must be ordered and shorter than one year")
        if self.adjusted and self.timeframe not in ("D", "W", "M"):
            raise ValueError("Adjusted prices support only daily, weekly and monthly bars")
        return self


class RealtimeMeta(APIModel):
    source: Literal["Fugle"] = "Fugle"
    source_url: str
    retrieved_at: datetime
    usage: Literal["private_development", "display_authorized"]
    units: Literal["source_native"] = "source_native"
    is_trial: bool | None = None
    transport: Literal["rest_snapshot"] = "rest_snapshot"
    # Retrieval time does not establish the exchange event time or latency.
    freshness: Literal["unknown"] = "unknown"


class RealtimeResponse(APIModel):
    data: dict[str, JsonValue] = Field(
        description="Native provider fields; prices keep exact strings"
    )
    meta: RealtimeMeta
