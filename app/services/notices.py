"""Sequential, bounded official notice retrieval and calendar export."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser

from app.schemas.common import Pagination
from app.schemas.notices import (
    CalendarEvent,
    CalendarKind,
    CalendarQuery,
    CalendarResponse,
    NoticeSource,
    RiskKind,
    RiskNotice,
    RiskQuery,
    RiskResponse,
)
from app.services.errors import ServiceError
from app.services.stocks import required_text
from app.services.twse import TwseService, invalid_data
from app.services.twse_catalog import BY_ID

SOURCE_TIMEOUT = 15  # Three sequential notice sources fit the gateway's 60s budget.
CALENDAR_SOURCES: dict[CalendarKind, str] = {
    "ex_dividend": "ex-dividend",
    "market": "market-calendar",
}
RISK_SOURCES: dict[RiskKind, str] = {
    "attention": "attention-securities",
    "disposition": "disposition-securities",
    "suspension": "suspended-securities",
}
EMPTY_ATTENTION_COLUMNS = {
    "Number",
    "Code",
    "Name",
    "NumberOfAnnouncement",
    "TradingInfoForAttention",
    "Date",
    "ClosingPrice",
    "PE",
}
Row = dict[str, str | None]


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")


def plain_text(value: str | None) -> str:
    parser = PlainText()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.parts).strip()


def source_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = value.strip()
    if re.fullmatch(r"[0-9]{7,8}", text):
        year, month, day = text[:-4], text[-4:-2], text[-2:]
    else:
        match = re.fullmatch(r"([0-9]{1,4})[./年-]([0-9]{1,2})[./月-]([0-9]{1,2})日?", text)
        if not match:
            return None
        year, month, day = match.groups()
    try:
        parsed = date(int(year) + (1911 if len(year) <= 3 else 0), int(month), int(day))
        return parsed if parsed < date.max else None
    except ValueError:
        return None


def identity(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def stock_code(row: Row) -> str:
    code = required_text(row, "Code")
    if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
        raise invalid_data()
    return code


def calendar_events(key: str, rows: list[Row]) -> list[CalendarEvent]:
    result: dict[str, CalendarEvent] = {}
    kind: CalendarKind = "ex_dividend" if key == "ex-dividend" else "market"
    for row in rows:
        name = required_text(row, "Name")
        code = stock_code(row) if kind == "ex_dividend" else None
        day = source_date(row.get("Date"))
        event_type = row.get("Exdividend") or "除權息"
        title = f"{code} {name} · 除權息（{event_type}）" if code else name
        description = (
            "；".join(
                f"{label}：{plain_text(row.get(field)) or '來源未提供'}"
                for label, field in (
                    ("除權息", "Exdividend"),
                    ("現金股利", "CashDividend"),
                    ("無償配股率", "StockDividendRatio"),
                    ("現金增資配股率", "SubscriptionRatio"),
                )
            )
            if code
            else plain_text(row.get("Description"))
        )
        event = CalendarEvent(
            id=identity(
                key,
                day.isoformat() if day else row.get("Date") or "",
                code or name,
                event_type if code else "",
            ),
            kind=kind,
            date=day,
            source_date=row.get("Date"),
            code=code,
            name=name,
            title=title,
            description=description,
            fields=row,
            source_url=BY_ID[key].source_url,
        )
        previous = result.get(event.id)
        if previous is not None and previous.fields != event.fields:
            raise invalid_data()
        result[event.id] = event
    return list(result.values())


def risk_notices(key: str, rows: list[Row]) -> list[RiskNotice]:
    # Official empty snapshot: zero-count record with blank identity/text fields.
    # Recognize only this exact shape; malformed identities must still fail.
    if (
        key == "attention-securities"
        and len(rows) == 1
        and set(rows[0]) == EMPTY_ATTENTION_COLUMNS
        and rows[0]["Number"] == "0"
        and all(
            rows[0][field] in {None, ""}
            for field in ("Code", "Name", "Date", "TradingInfoForAttention")
        )
        and all(
            rows[0][field] in {None, ""} or re.fullmatch(r"0+(?:\.0+)?", rows[0][field] or "")
            for field in ("NumberOfAnnouncement", "ClosingPrice", "PE")
        )
    ):
        return []
    kind = next(kind for kind, dataset in RISK_SOURCES.items() if dataset == key)
    result: dict[str, RiskNotice] = {}
    for row in rows:
        code, name = stock_code(row), required_text(row, "Name")
        fields: tuple[tuple[str, str], ...]
        if kind == "attention":
            fields = (("注意交易資訊", "TradingInfoForAttention"),)
            date_label = row.get("Date")
        elif kind == "disposition":
            fields = (
                ("處置期間", "DispositionPeriod"),
                ("處置條件", "ReasonsOfDisposition"),
                ("處置措施", "DispositionMeasures"),
            )
            date_label = row.get("Date")
        else:
            fields = (
                ("暫停交易日期", "TradingHaltDate"),
                ("暫停交易時間", "TradingHaltTime"),
                ("恢復交易日期", "TradingResumptionDate"),
                ("恢復交易時間", "TradingResumptionTime"),
            )
            date_label = row.get("TradingHaltDate")
        item = RiskNotice(
            id=identity(key, json.dumps(row, sort_keys=True, ensure_ascii=False)),
            kind=kind,
            code=code,
            name=name,
            announced_on=source_date(row.get("Date")),
            date_label=date_label,
            description="\n".join(
                f"{label}：{plain_text(row[field])}" for label, field in fields if row.get(field)
            ),
            fields=row,
            source_url=BY_ID[key].source_url,
        )
        result[item.id] = item
    return list(result.values())


def ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_ics_line(line: str) -> str:
    """RFC 5545: max 75 UTF-8 octets, including continuation whitespace."""
    chunks, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            chunks.append(current)
            current, size = " ", 1
        current += char
        size += width
    chunks.append(current)
    return "\r\n".join(chunks)


def render_calendar(events: list[CalendarEvent]) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TW Stock Analysis//Official Events//ZH-TW",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:TWSE 官方事件",
    ]
    for event in events:
        if event.date is None:
            continue
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event.id}@tw-stock-analysis",
                f"DTSTAMP:{timestamp}",
                "DTSTART;VALUE=DATE:" + event.date.isoformat().replace("-", ""),
                "DTEND;VALUE=DATE:" + (event.date + timedelta(days=1)).isoformat().replace("-", ""),
                "SUMMARY:" + ics_text(event.title),
                "DESCRIPTION:"
                + ics_text(
                    event.description
                    + "\n來源："
                    + event.source_url
                    + "\n僅為本次官方公告快照，請另確認後續異動。"
                ),
                "URL:" + event.source_url,
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


class NoticeService:
    def __init__(self, twse: TwseService):
        self.twse = twse

    async def fetch_sources(
        self, keys: list[str], *, calendar: bool
    ) -> tuple[list[CalendarEvent], list[RiskNotice], list[NoticeSource]]:
        sources: list[NoticeSource] = []
        calendar_items: list[CalendarEvent] = []
        risk_items: list[RiskNotice] = []
        for key in keys:
            dataset = BY_ID[key]
            try:
                async with asyncio.timeout(SOURCE_TIMEOUT):
                    result = await self.twse.fetch(dataset)
                if calendar:
                    normalized_events = calendar_events(key, result.data)
                    calendar_items.extend(normalized_events)
                    count = len(normalized_events)
                else:
                    normalized_notices = risk_notices(key, result.data)
                    risk_items.extend(normalized_notices)
                    count = len(normalized_notices)
                sources.append(
                    NoticeSource(
                        id=key,
                        title=dataset.title,
                        source_url=dataset.source_url,
                        status="ok",
                        retrieved_at=result.meta.retrieved_at,
                        row_count=count,
                    )
                )
            except (ServiceError, TimeoutError) as error:
                sources.append(
                    NoticeSource(
                        id=key,
                        title=dataset.title,
                        source_url=dataset.source_url,
                        status="error",
                        error=error.message
                        if isinstance(error, ServiceError)
                        else "來源逾時，請稍後重新查詢。",
                    )
                )
        if all(source.status == "error" for source in sources):
            raise ServiceError(
                "SOURCE_UNAVAILABLE", "本次所選官方來源均無法取得，請稍後重新查詢。", 503
            )
        return calendar_items, risk_items, sources

    async def calendar(self, query: CalendarQuery, *, paginate: bool = True) -> CalendarResponse:
        keys = [key for kind, key in CALENDAR_SOURCES.items() if query.kind in {"all", kind}]
        events, _, sources = await self.fetch_sources(keys, calendar=True)
        term = query.q.strip().casefold()
        matches = [
            event
            for event in events
            if term in (event.code or "").casefold()
            or term in event.name.casefold()
            or term in event.description.casefold()
        ]
        undated = sum(event.date is None for event in matches)
        filtered = [
            event
            for event in matches
            if (query.from_date is None or (event.date and event.date >= query.from_date))
            and (query.to_date is None or (event.date and event.date <= query.to_date))
        ]
        filtered.sort(key=lambda event: (event.date or date.max, event.code or "", event.id))
        start = (query.page - 1) * query.limit
        return CalendarResponse(
            data=filtered[start : start + query.limit] if paginate else filtered,
            pagination=Pagination(page=query.page, limit=query.limit, total=len(filtered)),
            sources=sources,
            complete=all(source.status == "ok" for source in sources),
            undated=undated,
        )

    async def calendar_export(self, query: CalendarQuery) -> tuple[str, int]:
        result = await self.calendar(query, paginate=False)
        if not result.complete:
            raise ServiceError(
                "SOURCE_INCOMPLETE", "官方來源未全部取得，暫不產生日曆；請稍後重新下載。", 503
            )
        return render_calendar(result.data), result.undated

    async def risks(self, query: RiskQuery) -> RiskResponse:
        keys = [key for kind, key in RISK_SOURCES.items() if query.kind in {"all", kind}]
        _, items, sources = await self.fetch_sources(keys, calendar=False)
        matches = [item for item in items if query.code is None or item.code == query.code]
        matches.sort(
            key=lambda item: (source_date(item.date_label) or date.min, item.code, item.id),
            reverse=True,
        )
        start = (query.page - 1) * query.limit
        return RiskResponse(
            data=matches[start : start + query.limit],
            pagination=Pagination(page=query.page, limit=query.limit, total=len(matches)),
            sources=sources,
            complete=all(source.status == "ok" for source in sources),
        )
