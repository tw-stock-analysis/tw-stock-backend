from datetime import date
from typing import Literal

from pydantic import Field

from app.schemas.advanced_research import ResearchBlock
from app.schemas.analysis import (
    AnalysisQuery,
    AnalysisSection,
    FinMindMeta,
    HistoryPoint,
    InstitutionPoint,
    RevenuePoint,
)
from app.schemas.common import APIModel
from app.schemas.notices import CalendarResponse, RiskResponse
from app.schemas.stocks import StockMarginResponse, StockQuoteResponse, StockValuationResponse


class StudyQuery(AnalysisQuery):
    view: Literal["overview", "financial", "valuation", "chips", "notes"] = "overview"


class Metric(APIModel):
    key: str
    label: str
    value: str | None = None
    unit: str
    period: str | None = None
    status: Literal["ok", "insufficient", "not_applicable", "unavailable"] = "insufficient"
    explanation: str
    limitation: str


class CompanyStudy(APIModel):
    code: str
    name: str
    market: Literal["TWSE"] = "TWSE"
    view: Literal["overview", "financial", "valuation", "chips", "notes"]
    start_date: date
    end_date: date
    complete: bool = False
    quote: StockQuoteResponse
    change_percent: str | None = None
    industry: str | None = None
    classification_meta: FinMindMeta | None = None
    security_kind: Literal["company", "etf", "unknown"] = "unknown"
    valuation: AnalysisSection[StockValuationResponse] = Field(
        default_factory=lambda: AnalysisSection(status="not_requested")
    )
    margin: AnalysisSection[StockMarginResponse] = Field(
        default_factory=lambda: AnalysisSection(status="not_requested")
    )
    history: AnalysisSection[HistoryPoint] = Field(
        default_factory=lambda: AnalysisSection(status="not_requested")
    )
    revenue: AnalysisSection[RevenuePoint] = Field(
        default_factory=lambda: AnalysisSection(status="not_requested")
    )
    institutions: AnalysisSection[InstitutionPoint] = Field(
        default_factory=lambda: AnalysisSection(status="not_requested")
    )
    blocks: list[ResearchBlock] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    risks: RiskResponse | None = None
    calendar: CalendarResponse | None = None
    notes: list[str] = Field(default_factory=list)
