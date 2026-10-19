import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models import User, Watchlist, WatchlistItem

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1 with migrated PostgreSQL"
    ),
]


def make_user(**overrides):
    values = {"email": f"{uuid4().hex}@example.test", "password_hash": "$argon2id$test-only"}
    return User(**(values | overrides))


@pytest_asyncio.fixture
async def watchlist(db_session):
    user = make_user()
    record = Watchlist(name="長期追蹤", user=user)
    db_session.add(record)
    await db_session.flush()
    return record


async def test_user_owns_multiple_lists_and_stock_can_appear_in_each(db_session):
    owner = make_user(
        watchlists=[
            Watchlist(name="長期追蹤", items=[WatchlistItem(stock_code="0050")]),
            Watchlist(name="ETF", items=[WatchlistItem(stock_code="0050")]),
        ]
    )
    another = make_user(
        watchlists=[Watchlist(name="ETF", items=[WatchlistItem(stock_code="0050")])]
    )
    db_session.add_all([owner, another])
    await db_session.flush()
    db_session.expunge_all()

    lists = (
        await db_session.scalars(
            select(Watchlist)
            .where(Watchlist.user_id == owner.id)
            .options(selectinload(Watchlist.items))
        )
    ).all()
    assert {record.name for record in lists} == {"長期追蹤", "ETF"}
    assert all(record.user_id == owner.id for record in lists)
    assert [record.items[0].stock_code for record in lists] == ["0050", "0050"]
    assert await db_session.scalar(select(func.count()).select_from(WatchlistItem)) == 3


async def test_name_normalization_and_update_timestamp(db_session):
    old_time = datetime.now(UTC) - timedelta(days=1)
    record = Watchlist(name="  ETF  ", user=make_user(), updated_at=old_time)
    db_session.add(record)
    await db_session.flush()
    assert record.name == "ETF"
    assert record.created_at.utcoffset() == timedelta(0)

    record.name = "  長期追蹤  "
    await db_session.flush()
    await db_session.refresh(record)
    assert record.name == "長期追蹤"
    assert record.updated_at > old_time


@pytest.mark.parametrize("code", ["0050", "00632R", "2330"])
async def test_item_identifier_normalization_and_defaults(db_session, watchlist, code):
    record = WatchlistItem(
        watchlist_id=watchlist.id, market=" twse ", stock_code=f" {code.lower()} "
    )
    db_session.add(record)
    await db_session.flush()
    key = (watchlist.id, "TWSE", code)
    db_session.expunge_all()
    loaded = await db_session.get(WatchlistItem, key)
    assert loaded.stock_code == code
    assert loaded.market == "TWSE"
    assert loaded.position == 0
    assert loaded.created_at.utcoffset() == timedelta(0)


async def test_same_market_and_stock_cannot_repeat_in_a_list(db_session, watchlist):
    db_session.add(WatchlistItem(watchlist_id=watchlist.id, stock_code="0050"))
    await db_session.flush()
    with pytest.raises(IntegrityError, match="pk_watchlist_items"):
        await db_session.execute(
            insert(WatchlistItem).values(watchlist_id=watchlist.id, stock_code="0050")
        )


async def test_order_is_stable_and_positions_can_be_swapped(db_session, watchlist):
    db_session.add_all(
        [
            WatchlistItem(watchlist_id=watchlist.id, stock_code="2330", position=2),
            WatchlistItem(watchlist_id=watchlist.id, stock_code="00632R", position=0),
            WatchlistItem(watchlist_id=watchlist.id, stock_code="0050", position=0),
        ]
    )
    await db_session.flush()
    db_session.expunge_all()
    query = (
        select(Watchlist).where(Watchlist.id == watchlist.id).options(selectinload(Watchlist.items))
    )
    loaded = await db_session.scalar(query)
    assert [item.stock_code for item in loaded.items] == ["0050", "00632R", "2330"]
    for item in loaded.items:
        if item.stock_code == "0050":
            item.position = 2
        elif item.stock_code == "2330":
            item.position = 0
    await db_session.flush()
    db_session.expunge_all()
    reordered = await db_session.scalar(query)
    assert [item.stock_code for item in reordered.items] == ["00632R", "2330", "0050"]


async def test_removing_relationship_item_deletes_only_that_item(db_session, watchlist):
    db_session.add_all(
        [
            WatchlistItem(watchlist_id=watchlist.id, stock_code="0050"),
            WatchlistItem(watchlist_id=watchlist.id, stock_code="2330"),
        ]
    )
    await db_session.flush()
    loaded = await db_session.scalar(
        select(Watchlist).where(Watchlist.id == watchlist.id).options(selectinload(Watchlist.items))
    )
    loaded.items.remove(next(item for item in loaded.items if item.stock_code == "0050"))
    await db_session.flush()
    assert await db_session.get(WatchlistItem, (watchlist.id, "TWSE", "0050")) is None
    assert await db_session.get(WatchlistItem, (watchlist.id, "TWSE", "2330")) is not None


@pytest.mark.parametrize("name", ["", "   ", " padded "])
async def test_database_rejects_blank_or_untrimmed_names(db_session, watchlist, name):
    with pytest.raises(IntegrityError, match="name_not_blank"):
        await db_session.execute(
            insert(Watchlist.__table__).values(user_id=watchlist.user_id, name=name)
        )


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"stock_code": "50"}, "stock_code_format"),
        ({"stock_code": "AB-C"}, "stock_code_format"),
        ({"stock_code": "00ab"}, "stock_code_format"),
        ({"stock_code": " 0050"}, "stock_code_format"),
        ({"stock_code": "漢字測試"}, "stock_code_format"),
        ({"market": "TPEX"}, "market_allowed"),
        ({"position": -1}, "position_nonnegative"),
    ],
)
async def test_database_item_constraints(db_session, watchlist, values, constraint):
    row = {"watchlist_id": watchlist.id, "stock_code": "0050"} | values
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.execute(insert(WatchlistItem.__table__).values(**row))


@pytest.mark.parametrize("kind", ["watchlist", "item"])
async def test_orphaned_parent_rejected(db_session, kind):
    record = (
        Watchlist(user_id=uuid4(), name="ETF")
        if kind == "watchlist"
        else WatchlistItem(watchlist_id=uuid4(), stock_code="0050")
    )
    db_session.add(record)
    with pytest.raises(IntegrityError, match="foreign key constraint"):
        await db_session.flush()


@pytest.mark.parametrize("parent", ["user", "watchlist"])
@pytest.mark.parametrize("load_relationships", [False, True])
async def test_cascade_delete_preserves_other_users_lists(db_session, parent, load_relationships):
    owner = make_user(watchlists=[Watchlist(name="ETF", items=[WatchlistItem(stock_code="0050")])])
    another = make_user(
        watchlists=[Watchlist(name="ETF", items=[WatchlistItem(stock_code="0050")])]
    )
    db_session.add_all([owner, another])
    await db_session.flush()
    target_id = owner.watchlists[0].id
    surviving_id = another.watchlists[0].id
    db_session.expunge_all()

    if parent == "user":
        query = select(User).where(User.id == owner.id)
        if load_relationships:
            query = query.options(selectinload(User.watchlists).selectinload(Watchlist.items))
    else:
        query = select(Watchlist).where(Watchlist.id == target_id)
        if load_relationships:
            query = query.options(selectinload(Watchlist.items))
    loaded = await db_session.scalar(query)
    await db_session.delete(loaded)
    await db_session.flush()

    assert await db_session.get(Watchlist, target_id) is None
    assert await db_session.get(WatchlistItem, (target_id, "TWSE", "0050")) is None
    assert await db_session.get(Watchlist, surviving_id) is not None
    assert await db_session.get(WatchlistItem, (surviving_id, "TWSE", "0050")) is not None
