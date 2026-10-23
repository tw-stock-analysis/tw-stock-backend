"""Request-local calculations only. No database, retained snapshots or retries."""

import asyncio
import csv
import io
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from app.schemas.common import Pagination
from app.schemas.research import (
    ComparisonItem,
    ComparisonQuery,
    ComparisonResponse,
    OverviewResponse,
    ResearchQuote,
    ScreenerQuery,
    ScreenerResponse,
)
from app.schemas.stocks import StockValuation
from app.schemas.twse import TradingResponse
from app.services.errors import ServiceError
from app.services.stocks import indexed_rows, quote, required_text, source_meta
from app.services.twse import TwseService
from app.services.twse_catalog import BY_ID

NUMBER = re.compile(r"^[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?$")
VALUATION_TIMEOUT = 20


def decimal_value(value: str | None) -> Decimal | None:
    if value is None or len(value) > 64 or not NUMBER.fullmatch(value.strip()):
        return None
    return Decimal(value.strip().replace(",", ""))


def change_percent(stock: ResearchQuote) -> Decimal | None:
    closing, change = decimal_value(stock.closing_price), decimal_value(stock.change)
    if closing is None or change is None or closing <= 0:
        return None
    with localcontext() as context:
        context.prec = 128
        previous = closing - change
        return change / previous * 100 if previous > 0 else None


def research_quote(row: dict[str, str | None]) -> ResearchQuote:
    stock = ResearchQuote(**quote(row).model_dump())
    percent = change_percent(stock)
    if percent is not None:
        with localcontext() as context:
            context.prec = 128
            try:
                rounded = percent.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                stock.change_percent = format(abs(rounded) if not rounded else rounded, "f")
            except InvalidOperation:
                pass
    return stock


def snapshot_meta(result: TradingResponse):
    meta = source_meta(result)
    dates = {row.get("Date") for row in result.data}
    if len(dates) == 1:
        meta.data_date = dates.pop()
    meta.note = "官方日成交快照，非盤中即時報價；日期請見各筆資料。取得時間不代表交易日。"
    return meta


def sort_stocks(stocks: list[ResearchQuote], field: str, descending: bool) -> list[ResearchQuote]:
    # Missing values stay last in both directions; equal numeric values use code order.
    ordered = sorted(stocks, key=lambda stock: stock.code)
    if field == "code":
        return list(reversed(ordered)) if descending else ordered

    def value(stock: ResearchQuote) -> Decimal | None:
        return (
            change_percent(stock)
            if field == "change_percent"
            else decimal_value(getattr(stock, field))
        )

    present = [stock for stock in ordered if value(stock) is not None]
    missing = [stock for stock in ordered if value(stock) is None]
    return (
        sorted(present, key=lambda stock: value(stock) or Decimal(0), reverse=descending) + missing
    )


def filter_stocks(stocks: list[ResearchQuote], query: ScreenerQuery) -> list[ResearchQuote]:
    term = query.q.strip().casefold()
    result = []
    for stock in stocks:
        if term not in stock.code.casefold() and term not in stock.name.casefold():
            continue
        price, percent, volume = (
            decimal_value(stock.closing_price),
            change_percent(stock),
            decimal_value(stock.trade_volume),
        )
        bounds = (
            (price, query.min_price, query.max_price),
            (percent, query.min_change, query.max_change),
            (volume, query.min_volume, None),
        )
        if any(
            (low is not None and (value is None or value < low))
            or (high is not None and (value is None or value > high))
            for value, low, high in bounds
        ):
            continue
        result.append(stock)
    return sort_stocks(result, query.sort, query.order == "desc")


def trading_date(value: str | None) -> date | None:
    if value is None or not re.fullmatch(r"[0-9]{7,8}", value):
        return None
    year = int(value[:-4]) + (1911 if len(value) == 7 else 0)
    try:
        return date(year, int(value[-4:-2]), int(value[-2:]))
    except ValueError:
        return None


def csv_cell(value: str | None, *, identifier: bool = False) -> str:
    text = value or ""
    # Keep leading-zero identifiers and prevent spreadsheet formula execution.
    if text and (
        identifier
        or text.lstrip().startswith(("=", "+", "-", "@"))
        or text.startswith(("\t", "\r", "\n"))
    ):
        return "'" + text
    return text


def screener_csv(stocks: list[ResearchQuote]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "證券代碼",
            "名稱",
            "資料日期",
            "收盤價",
            "漲跌價差",
            "漲跌幅(%)",
            "成交股數",
            "成交金額(元)",
        ]
    )
    for stock in stocks:
        writer.writerow(
            [
                csv_cell(stock.code, identifier=True),
                csv_cell(stock.name),
                csv_cell(stock.date),
                csv_cell(stock.closing_price),
                csv_cell(stock.change),
                csv_cell(stock.change_percent),
                csv_cell(stock.trade_volume),
                csv_cell(stock.trade_value),
            ]
        )
    return "\ufeff" + output.getvalue()


class ResearchService:
    def __init__(self, twse: TwseService):
        self.twse = twse

    async def snapshot(self):
        result = await self.twse.fetch(BY_ID["daily-prices"])
        return [research_quote(row) for row in indexed_rows(result).values()], snapshot_meta(result)

    async def screen(self, query: ScreenerQuery) -> ScreenerResponse:
        stocks, meta = await self.snapshot()
        matches = filter_stocks(stocks, query)
        start = (query.page - 1) * query.limit
        return ScreenerResponse(
            data=matches[start : start + query.limit],
            pagination=Pagination(page=query.page, limit=query.limit, total=len(matches)),
            meta=meta,
        )

    async def export(self, query: ScreenerQuery) -> str:
        stocks, _ = await self.snapshot()
        return screener_csv(filter_stocks(stocks, query))

    async def compare(self, query: ComparisonQuery) -> ComparisonResponse:
        stocks, meta = await self.snapshot()
        by_code = {stock.code: stock for stock in stocks}
        data = [
            ComparisonItem(code=code, quote=by_code.get(code), valuation=None)
            for code in query.codes.split(",")
        ]
        response = ComparisonResponse(data=data, quote_meta=meta, valuation_meta=None)
        if not any(item.quote for item in data):
            return response
        # Bulk valuations follow the daily snapshot, never concurrent source requests.
        try:
            # Daily retrieval has a 30s bound; stay below the gateway's 60s budget.
            async with asyncio.timeout(VALUATION_TIMEOUT):
                result = await self.twse.fetch(BY_ID["valuations-by-code"])
            values = indexed_rows(result)
            response.valuation_meta = snapshot_meta(result)
            response.valuation_meta.note = "估值與成交可能為不同資料日期，請分別查看。"
            for item in data:
                row = values.get(item.code)
                if item.quote is not None and row is not None:
                    item.valuation = StockValuation(
                        code=item.code,
                        name=required_text(row, "Name"),
                        date=row.get("Date"),
                        pe_ratio=row.get("PEratio"),
                        dividend_yield=row.get("DividendYield"),
                        pb_ratio=row.get("PBratio"),
                    )
        except (ServiceError, TimeoutError) as error:
            response.valuation_meta = None
            response.valuation_error = (
                error.message
                if isinstance(error, ServiceError)
                else "估值來源逾時，請稍後重新取得。"
            )
            for item in data:
                item.valuation = None
        return response

    async def overview(self) -> OverviewResponse:
        stocks, meta = await self.snapshot()
        latest = max((day for stock in stocks if (day := trading_date(stock.date))), default=None)
        eligible = [stock for stock in stocks if latest and trading_date(stock.date) == latest]
        if eligible:
            meta.data_date = eligible[0].date
        else:
            meta.data_date = None
        meta.note = "只統計本次快照最新有效交易日的上市證券（含 ETF 等），非僅普通股。"
        gainers, losers, unchanged, unknown = [], [], 0, 0
        for stock in eligible:
            change = change_percent(stock)
            if change is None:
                unknown += 1
            elif change > 0:
                gainers.append(stock)
            elif change < 0:
                losers.append(stock)
            else:
                unchanged += 1
        volume = [stock for stock in eligible if (decimal_value(stock.trade_volume) or 0) > 0]
        return OverviewResponse(
            total=len(eligible),
            rising=len(gainers),
            declining=len(losers),
            unchanged=unchanged,
            unclassified=unknown,
            excluded=len(stocks) - len(eligible),
            gainers=sort_stocks(gainers, "change_percent", True)[:10],
            losers=sort_stocks(losers, "change_percent", False)[:10],
            volume_leaders=sort_stocks(volume, "trade_volume", True)[:10],
            meta=meta,
        )
