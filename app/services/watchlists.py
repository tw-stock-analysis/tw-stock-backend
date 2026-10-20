"""Owner-scoped watchlists, serialized capacity checks, and fresh TWSE validation."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Watchlist, WatchlistItem
from app.services.account_state import SessionFactory, database_now, require_user, transaction
from app.services.errors import ServiceError
from app.services.stocks import StockService

MAX_LISTS = 10
MAX_ITEMS = 100


@dataclass(frozen=True)
class WatchlistStock:
    market: str
    stock_code: str
    position: int


@dataclass(frozen=True)
class WatchlistView:
    id: UUID
    name: str
    updated_at: datetime
    items: tuple[WatchlistStock, ...]


def clean_name(name: str) -> str:
    result = name.strip()
    if not result or len(result) > 100:
        raise ServiceError("WATCHLIST_NAME_INVALID", "清單名稱須為 1–100 字。", 422)
    return result


def clean_code(code: str, market: str = "TWSE") -> str:
    result = code.strip().upper()
    if market.strip().upper() != "TWSE" or not re.fullmatch(r"[0-9A-Z]{4,6}", result):
        raise ServiceError("STOCK_CODE_INVALID", "目前僅支援 TWSE 的 4–6 碼英數代碼。", 422)
    return result


async def owned_list(
    session: AsyncSession, user_id: UUID, list_id: UUID, *, lock: bool = False
) -> Watchlist:
    query = select(Watchlist).where(Watchlist.id == list_id, Watchlist.user_id == user_id)
    if lock:
        query = query.with_for_update()
    record = await session.scalar(query)
    if record is None:
        raise ServiceError("WATCHLIST_NOT_FOUND", "找不到自選清單。", 404)
    return record


async def ordered_items(session: AsyncSession, list_id: UUID) -> list[WatchlistItem]:
    return list(
        await session.scalars(
            select(WatchlistItem)
            .where(WatchlistItem.watchlist_id == list_id)
            .order_by(WatchlistItem.position, WatchlistItem.market, WatchlistItem.stock_code)
        )
    )


class WatchlistService:
    def __init__(self, factory: SessionFactory, stocks: StockService):
        self.factory = factory
        self.stocks = stocks

    async def create(self, user_id: UUID, name: str) -> UUID:
        name = clean_name(name)
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            count = await session.scalar(
                select(func.count()).select_from(Watchlist).where(Watchlist.user_id == user_id)
            )
            if count is not None and count >= MAX_LISTS:
                raise ServiceError("WATCHLIST_LIMIT_REACHED", "每人最多 10 份自選清單。", 409)
            record = Watchlist(user_id=user_id, name=name)
            session.add(record)
            await session.flush()
            result = record.id
        return result

    async def list(self, user_id: UUID) -> list[WatchlistView]:
        async with self.factory() as session:
            await require_user(session, user_id, lock=True)
            records = await session.scalars(
                select(Watchlist)
                .where(Watchlist.user_id == user_id)
                .order_by(Watchlist.created_at, Watchlist.id)
            )
            result = []
            for record in records:
                items = await ordered_items(session, record.id)
                result.append(
                    WatchlistView(
                        record.id,
                        record.name,
                        record.updated_at,
                        tuple(
                            WatchlistStock(item.market, item.stock_code, item.position)
                            for item in items
                        ),
                    )
                )
            return result

    async def get(self, user_id: UUID, list_id: UUID) -> WatchlistView:
        async with self.factory() as session:
            await require_user(session, user_id, lock=True)
            record = await owned_list(session, user_id, list_id)
            items = await ordered_items(session, list_id)
            return WatchlistView(
                record.id,
                record.name,
                record.updated_at,
                tuple(
                    WatchlistStock(item.market, item.stock_code, item.position) for item in items
                ),
            )

    async def rename(self, user_id: UUID, list_id: UUID, name: str) -> None:
        name = clean_name(name)
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            record = await owned_list(session, user_id, list_id, lock=True)
            record.name = name
            record.updated_at = await database_now(session)

    async def delete(self, user_id: UUID, list_id: UUID) -> None:
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            record = await owned_list(session, user_id, list_id, lock=True)
            await session.delete(record)

    async def add_item(
        self, user_id: UUID, list_id: UUID, stock_code: str, *, market: str = "TWSE"
    ) -> None:
        code = clean_code(stock_code, market)
        # Authorize first, then release the DB transaction before network I/O.
        async with self.factory() as session:
            await require_user(session, user_id)
            await owned_list(session, user_id, list_id)
        await self.stocks.quote(code)
        # Recheck owner/account after retrieval; deletion or concurrent additions can race it.
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            record = await owned_list(session, user_id, list_id, lock=True)
            items = await ordered_items(session, list_id)
            if any(item.stock_code == code and item.market == "TWSE" for item in items):
                raise ServiceError("WATCHLIST_ITEM_EXISTS", "清單已有此股票。", 409)
            if len(items) >= MAX_ITEMS:
                raise ServiceError("WATCHLIST_ITEM_LIMIT_REACHED", "每份清單最多 100 檔股票。", 409)
            position = max((item.position for item in items), default=-1) + 1
            session.add(
                WatchlistItem(
                    watchlist_id=list_id,
                    market="TWSE",
                    stock_code=code,
                    position=position,
                )
            )
            record.updated_at = await database_now(session)

    async def remove_item(
        self, user_id: UUID, list_id: UUID, stock_code: str, *, market: str = "TWSE"
    ) -> None:
        code = clean_code(stock_code, market)
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            record = await owned_list(session, user_id, list_id, lock=True)
            items = await ordered_items(session, list_id)
            target = next((item for item in items if item.stock_code == code), None)
            if target is None:
                raise ServiceError("WATCHLIST_ITEM_NOT_FOUND", "清單沒有此股票。", 404)
            await session.delete(target)
            remaining = [item for item in items if item is not target]
            for position, item in enumerate(remaining):
                item.position = position
            record.updated_at = await database_now(session)

    async def reorder(self, user_id: UUID, list_id: UUID, stock_codes: Sequence[str]) -> None:
        codes = [clean_code(code) for code in stock_codes]
        if len(codes) != len(set(codes)):
            raise ServiceError("WATCHLIST_ORDER_INVALID", "排序不得包含重複股票。", 422)
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            record = await owned_list(session, user_id, list_id, lock=True)
            items = await ordered_items(session, list_id)
            by_code = {item.stock_code: item for item in items}
            if set(codes) != set(by_code):
                raise ServiceError(
                    "WATCHLIST_ORDER_INVALID", "排序須包含清單內全部且僅有的股票。", 409
                )
            for position, code in enumerate(codes):
                by_code[code].position = position
            record.updated_at = await database_now(session)
