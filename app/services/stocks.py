"""Fresh source retrieval followed by in-memory filtering; no cache or database."""

from typing import Protocol

from app.schemas.common import Pagination
from app.schemas.stocks import (
    StockMargin,
    StockMarginResponse,
    StockQuote,
    StockQuoteResponse,
    StockSearchQuery,
    StockSearchResponse,
    StockSource,
    StockValuation,
    StockValuationResponse,
)
from app.schemas.twse import TradingResponse
from app.services.errors import ServiceError
from app.services.twse import TwseService, invalid_data
from app.services.twse_catalog import BY_ID

Row = dict[str, str | None]


class QuoteProvider(Protocol):
    async def quote(self, code: str) -> StockQuoteResponse: ...


def required_text(row: Row, key: str) -> str:
    value = row.get(key)
    if not value:
        raise invalid_data()
    return value


def source_meta(result: TradingResponse, row: Row | None = None) -> StockSource:
    return StockSource(
        source_url=result.meta.source_url,
        retrieved_at=result.meta.retrieved_at,
        data_date=row.get("Date") if row else None,
    )


def indexed_rows(result: TradingResponse, code_key: str = "Code", name_key: str = "Name"):
    """Fail on missing identities or duplicates rather than attaching a wrong quote."""
    indexed: dict[str, Row] = {}
    for row in result.data:
        code, name = row.get(code_key), row.get(name_key)
        if not code or not name or code in indexed:
            raise invalid_data()
        indexed[code] = row
    return indexed


def quote(row: Row) -> StockQuote:
    return StockQuote(
        code=required_text(row, "Code"),
        name=required_text(row, "Name"),
        date=row.get("Date"),
        opening_price=row.get("OpeningPrice"),
        highest_price=row.get("HighestPrice"),
        lowest_price=row.get("LowestPrice"),
        closing_price=row.get("ClosingPrice"),
        change=row.get("Change"),
        trade_volume=row.get("TradeVolume"),
        trade_value=row.get("TradeValue"),
        transactions=row.get("Transaction"),
    )


class StockService:
    def __init__(self, twse: TwseService):
        self.twse = twse

    async def search(self, query: StockSearchQuery) -> StockSearchResponse:
        result = await self.twse.fetch(BY_ID["daily-prices"])
        rows = indexed_rows(result).values()
        term = query.q.strip().casefold()
        matches = [
            row
            for row in rows
            if term in (row["Code"] or "").casefold() or term in (row["Name"] or "").casefold()
        ]
        start = (query.page - 1) * query.limit
        meta = source_meta(result)
        dates = {row.get("Date") for row in result.data}
        if len(dates) == 1:
            meta.data_date = dates.pop()
        else:
            meta.note = "資料日期請見各筆資料列；取得時間不代表資料日期。"
        return StockSearchResponse(
            data=[quote(row) for row in matches[start : start + query.limit]],
            pagination=Pagination(page=query.page, limit=query.limit, total=len(matches)),
            meta=meta,
        )

    async def quote(self, code: str) -> StockQuoteResponse:
        result = await self.twse.fetch(BY_ID["daily-prices"])
        row = indexed_rows(result).get(code)
        if row is None:
            raise ServiceError("STOCK_NOT_FOUND", "本次 TWSE 日成交資料中找不到此代碼。", 404)
        return StockQuoteResponse(data=quote(row), meta=source_meta(result, row))

    async def valuation(self, code: str) -> StockValuationResponse:
        result = await self.twse.fetch(BY_ID["valuations-by-code"])
        row = indexed_rows(result).get(code)
        data = None
        if row is not None:
            data = StockValuation(
                code=code,
                name=required_text(row, "Name"),
                date=row.get("Date"),
                pe_ratio=row.get("PEratio"),
                dividend_yield=row.get("DividendYield"),
                pb_ratio=row.get("PBratio"),
            )
        return StockValuationResponse(data=data, meta=source_meta(result, row))

    async def margin(self, code: str) -> StockMarginResponse:
        result = await self.twse.fetch(BY_ID["margin-balances"])
        row = indexed_rows(result, "股票代號", "股票名稱").get(code)
        data = None
        if row is not None:
            data = StockMargin(
                code=code,
                name=required_text(row, "股票名稱"),
                margin_buy=row.get("融資買進"),
                margin_sell=row.get("融資賣出"),
                margin_repayment=row.get("融資現金償還"),
                margin_previous_balance=row.get("融資前日餘額"),
                margin_balance=row.get("融資今日餘額"),
                margin_limit=row.get("融資限額"),
                short_sell=row.get("融券賣出"),
                short_buy=row.get("融券買進"),
                short_repayment=row.get("融券現券償還"),
                short_previous_balance=row.get("融券前日餘額"),
                short_balance=row.get("融券今日餘額"),
                short_limit=row.get("融券限額"),
                offsetting=row.get("資券互抵"),
                note=row.get("註記"),
            )
        meta = source_meta(result)
        meta.note = "來源未提供資料日期；資券數值沿用來源單位，不以取得日期補上交易日。"
        return StockMarginResponse(data=data, meta=meta)
