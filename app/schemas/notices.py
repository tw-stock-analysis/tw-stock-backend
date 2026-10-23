"""Calendar events and official notices, without persistence or risk scoring."""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.common import APIModel, Pagination

CalendarKind = Literal["ex_dividend", "market"]
RiskKind = Literal["attention", "disposition", "suspension"]


class CalendarQuery(APIModel):
    kind: Literal["all", "ex_dividend", "market"] = "all"
    q: str = Field(default="", max_length=100)
    from_date: date | None = None
    to_date: date | None = None
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("from_date", "to_date", mode="before")
    @classmethod
    def iso_date(cls, value):
        if isinstance(value, str) and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("請使用 YYYY-MM-DD 日期。")
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("起日不可晚於迄日。")
        return self


class RiskQuery(APIModel):
    kind: Literal["all", "attention", "disposition", "suspension"] = "all"
    code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{4,6}$")
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=100)


class NoticeSource(APIModel):
    id: str
    title: str
    source_url: str
    status: Literal["ok", "error"]
    retrieved_at: datetime | None = None
    row_count: int | None = None
    error: str | None = None


class CalendarEvent(APIModel):
    id: str
    kind: CalendarKind
    date: date | None
    source_date: str | None
    code: str | None = None
    name: str
    title: str
    description: str
    fields: dict[str, str | None]
    source_url: str


class CalendarResponse(APIModel):
    data: list[CalendarEvent]
    pagination: Pagination
    sources: list[NoticeSource]
    complete: bool
    undated: int


class RiskNotice(APIModel):
    id: str
    kind: RiskKind
    code: str
    name: str
    announced_on: date | None = None
    date_label: str | None = None
    description: str
    fields: dict[str, str | None]
    source_url: str


class RiskResponse(APIModel):
    data: list[RiskNotice]
    pagination: Pagination
    sources: list[NoticeSource]
    complete: bool
    note: str = (
        "只彙整本次官方來源提供的公告，不判定目前生效狀態；未列出不代表無其他風險或必然可交易。"
    )
