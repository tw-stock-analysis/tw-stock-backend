"""A request-local multi-market catalogue assembled from current source references."""

import re
from collections import defaultdict
from datetime import date

from app.schemas.common import Pagination
from app.schemas.instruments import (
    Instrument,
    InstrumentRef,
    InstrumentSearch,
    InstrumentSearchResponse,
    Market,
    ReferenceRecord,
)
from app.schemas.stocks import StockQuote, StockQuoteResponse, StockSource
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, invalid_data
from app.services.stock_analysis import record_date

MARKETS: dict[str, Market] = {"twse": "TWSE", "tpex": "TPEX", "emerging": "EMERGING"}


class InstrumentService:
    def __init__(self, finmind: FinMindService):
        self.finmind = finmind

    async def catalogue(self) -> InstrumentSearchResponse:
        info, info_meta = await self.finmind.catalogue("TaiwanStockInfo")
        removed, removed_meta = await self.finmind.catalogue("TaiwanStockDelisting")
        records: dict[str, list[ReferenceRecord]] = defaultdict(list)
        industry: dict[tuple[str, str, date | None], str | None] = {}
        for row in info:
            code, name, market = (
                row.get("stock_id"),
                row.get("stock_name"),
                MARKETS.get(row.get("type") or ""),
            )
            if not code or not name or market is None:
                raise invalid_data()
            if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
                continue
            day = None if row.get("date") in (None, "", "None") else record_date(row)
            record = ReferenceRecord(name=name, market=market, source_date=day, kind="reference")
            if record not in records[code]:
                records[code].append(record)
            industry[(code, name, day)] = row.get("industry_category")
        for row in removed:
            code, name = row.get("stock_id"), row.get("stock_name")
            if not code or not name:
                raise invalid_data()
            if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
                continue
            record = ReferenceRecord(
                name=name, market="DELISTED", source_date=record_date(row), kind="delisting"
            )
            if record not in records[code]:
                records[code].append(record)
        result = []
        for code, history in records.items():
            history.sort(key=lambda r: (r.source_date or date.min, r.kind == "delisting"))
            latest = history[-1]
            delisted = [
                r.source_date
                for r in history
                if r.kind == "delisting" and r.source_date is not None
            ]
            # Equal-date conflicting market references cannot be silently resolved.
            same_date = [r for r in history if r.source_date == latest.source_date]
            ambiguous = (
                len({(r.name, r.market) for r in same_date}) > 1
                or len({r.name for r in history}) > 1
            )
            result.append(
                Instrument(
                    market=latest.market,
                    code=code,
                    name=latest.name,
                    industry=industry.get((code, latest.name, latest.source_date)),
                    reference_date=latest.source_date,
                    delisted_on=max(delisted) if delisted else None,
                    records=history,
                    historical_identity="ambiguous" if ambiguous else "source_limited",
                )
            )
        result.sort(key=lambda r: (r.market, r.code))
        return InstrumentSearchResponse(
            data=result,
            pagination=Pagination(page=1, limit=len(result) or 1, total=len(result)),
            sources=[info_meta, removed_meta],
        )

    async def search(self, query: InstrumentSearch) -> InstrumentSearchResponse:
        response = await self.catalogue()
        term = query.q.strip().casefold()
        matches = [
            r
            for r in response.data
            if (query.market == "ALL" or r.market == query.market)
            and (
                term in r.code.casefold() or any(term in item.name.casefold() for item in r.records)
            )
        ]
        offset = (query.page - 1) * query.limit
        response.data = matches[offset : offset + query.limit]
        response.pagination = Pagination(page=query.page, limit=query.limit, total=len(matches))
        return response

    @staticmethod
    def resolve(catalogue: InstrumentSearchResponse, ref: InstrumentRef, as_of: date) -> Instrument:
        item = next((item for item in catalogue.data if item.code == ref.code), None)
        if item is None or not any(r.market == ref.market for r in item.records):
            raise ServiceError("INSTRUMENT_NOT_FOUND", "來源參考名錄中找不到此市場／代碼。", 404)
        if item.historical_identity == "ambiguous" or len({r.name for r in item.records}) > 1:
            raise ServiceError(
                "INSTRUMENT_IDENTITY_AMBIGUOUS",
                "此代碼有衝突或不同名稱的歷史參考，無法安全確認同一證券。",
                409,
            )
        return item


class InstrumentIdentityAdapter:
    """Adapt a validated source reference to existing read-only research services."""

    def __init__(self, service: InstrumentService, ref: InstrumentRef, as_of: date):
        self.service, self.ref, self.as_of = service, ref, as_of
        self.instrument: Instrument | None = None

    async def quote(self, code: str) -> StockQuoteResponse:
        if code != self.ref.code:
            raise ServiceError("INSTRUMENT_NOT_FOUND", "證券代碼不符。", 404)
        catalogue = await self.service.catalogue()
        item = self.service.resolve(catalogue, self.ref, self.as_of)
        self.instrument = item
        return StockQuoteResponse(
            data=StockQuote(code=code, name=item.name),
            meta=StockSource(
                source="FinMind",
                source_url=catalogue.sources[0].source_url,
                retrieved_at=catalogue.sources[0].retrieved_at,
                data_date=item.reference_date.isoformat() if item.reference_date else None,
                note=item.note,
            ),
        )
