"""Bounded, fixed-topic research; never accepts arbitrary datasets or source URLs."""

from datetime import date, timedelta
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.analysis import AnalysisSection, taipei_today
from app.schemas.common import APIModel
from app.schemas.stocks import StockSource

ResearchTopic = Literal[
    "valuation", "dividends", "fill", "financials", "adjusted", "ownership", "brokers", "etf"
]


class AdvancedQuery(APIModel):
    topic: ResearchTopic
    start_date: date | None = None
    end_date: date = Field(default_factory=taipei_today)

    @model_validator(mode="after")
    def validate_range(self) -> "AdvancedQuery":
        if self.start_date is None:
            self.start_date = (
                self.end_date
                if self.topic == "brokers"
                else (self.end_date - timedelta(days=30 if self.topic == "etf" else 365))
            )
        if (
            self.start_date < date(1994, 10, 1)
            or self.start_date > self.end_date
            or self.end_date > taipei_today()
            or (self.end_date - self.start_date).days > 1830
            or (self.topic == "brokers" and self.start_date != self.end_date)
            or (self.topic == "etf" and (self.end_date - self.start_date).days > 30)
        ):
            raise ValueError("Use at most 1830 days; broker one date; ETF at most 31 days")
        return self


class ResearchColumn(APIModel):
    key: str
    label: str


class ResearchBlock(APIModel):
    id: str
    title: str
    required_plan: Literal["Free", "Backer", "Sponsor"] = "Free"
    columns: list[ResearchColumn]
    result: AnalysisSection[dict[str, str | None]]


class AdvancedResponse(APIModel):
    code: str
    name: str
    market: Literal["TWSE"] = "TWSE"
    topic: ResearchTopic
    start_date: date
    end_date: date
    complete: bool
    identity_source: StockSource
    notes: list[str]
    blocks: list[ResearchBlock]
