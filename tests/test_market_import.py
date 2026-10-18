from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models.market import DailyPrice, Instrument, MarketDataset
from app.services.errors import ServiceError
from app.services.market_import import publish_snapshot
from app.services.twse import parse_snapshot

pytestmark = pytest.mark.integration


async def test_repeated_import_keeps_ids_and_does_not_duplicate_rows(db_session, snapshot):
    await publish_snapshot(db_session, snapshot)
    first_ids = (await db_session.scalars(select(Instrument.id).order_by(Instrument.ticker))).all()
    await publish_snapshot(db_session, snapshot)
    assert (
        await db_session.scalars(select(Instrument.id).order_by(Instrument.ticker))
    ).all() == first_ids
    assert await db_session.scalar(select(func.count()).select_from(DailyPrice)) == 2
    assert await db_session.scalar(select(func.count()).select_from(MarketDataset)) == 2


async def test_revision_updates_one_day_without_changing_identity(db_session, source_rows):
    first = parse_snapshot(*source_rows)
    await publish_snapshot(db_session, first)
    master, prices = deepcopy(source_rows)
    prices[0]["ClosingPrice"] = "594.00"
    revised = parse_snapshot(master, prices)
    original_id = await db_session.scalar(select(Instrument.id).where(Instrument.ticker == "2330"))
    await publish_snapshot(db_session, revised)
    db_session.expire_all()
    row = await db_session.get(DailyPrice, (original_id, date(2024, 1, 2)))
    assert str(row.close) == "594.000000"
    assert row.data_version == revised.prices_version
    assert row.raw["ClosingPrice"] == "594.00"
    assert await db_session.scalar(select(func.count()).select_from(DailyPrice)) == 2


async def test_older_snapshot_cannot_replace_newer_published_data(db_session, snapshot):
    await publish_snapshot(db_session, snapshot)
    with pytest.raises(ServiceError, match="Older snapshots"):
        await publish_snapshot(db_session, replace(snapshot, prices_date=date(2024, 1, 1)))
    row = await db_session.get(MarketDataset, "twse_daily_prices")
    assert row.data_as_of == date(2024, 1, 2)


async def test_failed_publication_rolls_back_partial_master_updates(db_session, snapshot):
    await publish_snapshot(db_session, snapshot)
    instruments, prices = deepcopy(snapshot.instruments), deepcopy(snapshot.prices)
    instruments[0]["name"] = "This must be rolled back"
    prices[0]["ticker"] = "not-in-master"
    with pytest.raises(KeyError):
        async with db_session.begin_nested():
            await publish_snapshot(
                db_session, replace(snapshot, instruments=instruments, prices=prices)
            )
    db_session.expire_all()
    assert (
        await db_session.scalar(select(Instrument.name).where(Instrument.ticker == "2330"))
        == "台積電"
    )
