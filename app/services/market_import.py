"""Explicit, idempotent import job: python -m app.services.market_import."""

import asyncio
from uuid import uuid4

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.market import DailyPrice, Instrument, MarketDataset
from app.services.errors import ServiceError
from app.services.twse import DATASETS, OfficialSnapshot, TWSEProvider


async def publish_snapshot(session: AsyncSession, snapshot: OfficialSnapshot) -> None:
    # Serialize import jobs and atomically publish both datasets. Keep DB locks out of HTTP I/O.
    await session.execute(text("SELECT pg_advisory_xact_lock(84231501)"))
    previous = (await session.scalars(select(MarketDataset))).all()
    dates = {"twse_instruments": snapshot.master_date, "twse_daily_prices": snapshot.prices_date}
    if any(dates[row.key] < row.data_as_of for row in previous if row.key in dates):
        raise ServiceError(
            "SOURCE_DATE_REGRESSION", "Older snapshots cannot replace current data.", 503
        )
    for item in snapshot.instruments:
        statement = insert(Instrument).values(
            id=uuid4(),
            market="TWSE",
            source_date=snapshot.master_date,
            retrieved_at=snapshot.retrieved_at,
            data_version=snapshot.master_version,
            **item,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[Instrument.market, Instrument.ticker],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in (
                        "name",
                        "company_name",
                        "industry_code",
                        "source_date",
                        "retrieved_at",
                        "data_version",
                        "raw",
                    )
                },
            )
        )
    ids = {
        ticker: identifier
        for ticker, identifier in (
            await session.execute(
                select(Instrument.ticker, Instrument.id).where(Instrument.market == "TWSE"),
            )
        ).all()
    }
    for item in snapshot.prices:
        values = {key: value for key, value in item.items() if key != "ticker"}
        statement = insert(DailyPrice).values(
            instrument_id=ids[item["ticker"]],
            retrieved_at=snapshot.retrieved_at,
            data_version=snapshot.prices_version,
            **values,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[DailyPrice.instrument_id, DailyPrice.trade_date],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume_shares",
                        "turnover",
                        "transactions",
                        "change",
                        "retrieved_at",
                        "data_version",
                        "raw",
                    )
                },
            )
        )
    for key, version, count in (
        ("twse_instruments", snapshot.master_version, len(snapshot.instruments)),
        ("twse_daily_prices", snapshot.prices_version, len(snapshot.prices)),
    ):
        statement = insert(MarketDataset).values(
            key=key,
            source_url=DATASETS[key],
            data_as_of=dates[key],
            retrieved_at=snapshot.retrieved_at,
            data_version=version,
            record_count=count,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[MarketDataset.key],
                set_={
                    column: getattr(statement.excluded, column)
                    for column in (
                        "source_url",
                        "data_as_of",
                        "retrieved_at",
                        "data_version",
                        "record_count",
                    )
                },
            )
        )
    # Caller owns the transaction; a failed publication rolls back all changes.


async def run_import() -> None:
    async with httpx.AsyncClient(follow_redirects=False) as client:
        snapshot = await TWSEProvider(client).fetch()
    engine = create_async_engine(get_settings().sqlalchemy_url())
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            await publish_snapshot(session, snapshot)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run_import())
    except ServiceError as error:
        raise SystemExit(f"{error.code}: {error.message}") from None
    except (SQLAlchemyError, OSError, TimeoutError):
        raise SystemExit("DEPENDENCY_UNAVAILABLE: Database import failed.") from None
    print("Official TWSE instruments and daily prices imported.")
