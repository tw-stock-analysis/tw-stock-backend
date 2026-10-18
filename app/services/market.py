from typing import Literal, cast
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market import DailyPrice, Instrument, MarketDataset
from app.schemas.common import Pagination
from app.schemas.market import (
    DailyQuote,
    DataMeta,
    DatasetKey,
    DatasetStatus,
    InstrumentData,
    InstrumentListResponse,
    InstrumentQuery,
    MarketStatusResponse,
    OverviewData,
    OverviewResponse,
    QuoteData,
    QuotesResponse,
)
from app.services.errors import MarketDataUnavailable, ServiceError
from app.services.twse import DATASETS, MASTER_URL, PRICES_URL


def dataset_meta(row: MarketDataset) -> DataMeta:
    return DataMeta(
        source_url=row.source_url,
        data_as_of=row.data_as_of,
        retrieved_at=row.retrieved_at,
        data_version=row.data_version,
    )


class MarketService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def require_dataset(self, key: str) -> MarketDataset:
        dataset = await self.session.get(MarketDataset, key)
        if dataset is None:
            raise MarketDataUnavailable()
        return dataset

    async def list_instruments(self, query: InstrumentQuery) -> InstrumentListResponse:
        if query.market == "TPEX":
            raise ServiceError("MARKET_NOT_SUPPORTED", "This version supports TWSE only.", 422)
        dataset = await self.require_dataset("twse_instruments")
        conditions = [Instrument.market == "TWSE", Instrument.data_version == dataset.data_version]
        if query.q:
            # Escape LIKE wildcards so user input always means literal text.
            conditions.append(
                or_(
                    Instrument.ticker.contains(query.q, autoescape=True),
                    Instrument.name.contains(query.q, autoescape=True),
                    Instrument.company_name.contains(query.q, autoescape=True),
                )
            )
        total = await self.session.scalar(
            select(func.count())
            .select_from(Instrument)
            .where(
                *conditions,
            )
        )
        rank = case(
            (Instrument.ticker == query.q, 0),
            (Instrument.ticker.startswith(query.q, autoescape=True), 1),
            else_=2,
        )
        rows = (
            await self.session.scalars(
                select(Instrument)
                .where(*conditions)
                .order_by(rank, Instrument.ticker)
                .offset((query.page - 1) * query.limit)
                .limit(query.limit),
            )
        ).all()
        return InstrumentListResponse(
            data=[InstrumentData.model_validate(row) for row in rows],
            pagination=Pagination(page=query.page, limit=query.limit, total=total or 0),
            meta=dataset_meta(dataset),
        )

    async def quotes(self, ids: list[UUID]) -> QuotesResponse:
        await self.require_dataset("twse_daily_prices")
        existing = set(
            (
                await self.session.scalars(
                    select(Instrument.id).where(Instrument.id.in_(ids)),
                )
            ).all()
        )
        # PostgreSQL DISTINCT ON: one latest stored trading day per instrument, one batch query.
        rows = (
            await self.session.scalars(
                select(DailyPrice)
                .where(DailyPrice.instrument_id.in_(ids))
                .distinct(DailyPrice.instrument_id)
                .order_by(DailyPrice.instrument_id, DailyPrice.trade_date.desc()),
            )
        ).all()
        by_id = {row.instrument_id: row for row in rows}
        result = []
        for instrument_id in ids:
            if instrument_id not in existing:
                result.append(
                    QuoteData(
                        instrument_id=instrument_id,
                        status="not_found",
                        reason_code="INSTRUMENT_NOT_FOUND",
                    )
                )
            elif instrument_id not in by_id:
                result.append(
                    QuoteData(
                        instrument_id=instrument_id, status="no_data", reason_code="NO_DAILY_PRICE"
                    )
                )
            else:
                row = by_id[instrument_id]
                fields = (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume_shares",
                    "turnover",
                    "transactions",
                    "change",
                )
                values = {field: getattr(row, field) for field in fields}
                trade_status: Literal["unknown", "no_trade", "traded"] = (
                    "unknown"
                    if row.volume_shares is None
                    else "no_trade"
                    if row.volume_shares == 0
                    else "traded"
                )
                result.append(
                    QuoteData(
                        instrument_id=instrument_id,
                        status="available",
                        quote=DailyQuote(
                            trade_date=row.trade_date,
                            **values,
                            trade_status=trade_status,
                            missing_fields=[field for field in fields if values[field] is None],
                            missing_reasons={
                                field: "NOT_COMPARABLE"
                                if field == "change" and row.raw["Change"].strip().startswith("X")
                                else "NO_TRADE"
                                if trade_status == "no_trade"
                                and field in ("open", "high", "low", "close", "change")
                                else "SOURCE_MISSING"
                                for field in fields
                                if values[field] is None
                            },
                            meta=DataMeta(
                                source_url=PRICES_URL,
                                data_as_of=row.trade_date,
                                retrieved_at=row.retrieved_at,
                                data_version=row.data_version,
                            ),
                        ),
                    )
                )
        return QuotesResponse(data=result)

    async def overview(self, instrument_id: UUID) -> OverviewResponse:
        await self.require_dataset("twse_instruments")
        row = await self.session.get(Instrument, instrument_id)
        if row is None:
            raise ServiceError("RESOURCE_NOT_FOUND", "Instrument not found.", 404)
        quote = (await self.quotes([instrument_id])).data[0]
        return OverviewResponse(
            data=OverviewData(
                instrument=InstrumentData.model_validate(row),
                instrument_meta=DataMeta(
                    source_url=MASTER_URL,
                    data_as_of=row.source_date,
                    retrieved_at=row.retrieved_at,
                    data_version=row.data_version,
                ),
                quote=quote,
            )
        )

    async def status(self) -> MarketStatusResponse:
        rows = (await self.session.scalars(select(MarketDataset))).all()
        by_key = {row.key: row for row in rows}
        return MarketStatusResponse(
            data=[
                DatasetStatus(
                    dataset=cast(DatasetKey, key),
                    status="available",
                    record_count=by_key[key].record_count,
                    meta=dataset_meta(by_key[key]),
                )
                if key in by_key
                else DatasetStatus(dataset=cast(DatasetKey, key), status="not_loaded")
                for key in DATASETS
            ]
        )
