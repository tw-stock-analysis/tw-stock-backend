"""Virtual capital and user-input orders; no source prices are persisted."""

from decimal import Decimal, localcontext

from sqlalchemy import func, select

from app.models import PaperAccount, Watchlist, WatchlistItem, WorkspaceItem
from app.schemas.portfolio import LedgerEntry
from app.schemas.practice import AccountCreate, OrderCreate, PracticeOrder, ReflectionUpdate
from app.services.account_state import SessionFactory, database_now, require_user, transaction
from app.services.errors import ServiceError
from app.services.portfolio import ledger_positions


def ledger(orders: list[PracticeOrder]) -> list[LedgerEntry]:
    return [
        LedgerEntry(
            market=o.market,
            code=o.code,
            kind=o.side,
            date=o.date,
            quantity=Decimal(o.quantity),
            price=o.price,
            fees=o.fees,
            taxes=o.taxes,
        )
        for o in orders
    ]


def balances(initial: Decimal, orders: list[PracticeOrder]) -> dict:
    cash = initial
    last_day = None
    with localcontext() as context:
        context.prec = 64
        for order in orders:
            if last_day is not None and order.date < last_day:
                raise ServiceError(
                    "PRACTICE_DATE_ORDER", "練習交易日期不可早於前一筆；可另開歷史練習帳戶。", 422
                )
            last_day = order.date
            amount = order.price * order.quantity
            charges = order.fees + order.taxes
            cash += -amount - charges if order.side == "buy" else amount - charges
            if cash < 0:
                raise ServiceError(
                    "PRACTICE_CASH_INSUFFICIENT", "虛擬現金不足，請減少股數或檢查費稅。", 422
                )
        positions = ledger_positions(ledger(orders))
        return {
            "cash": format(cash, "f"),
            "realized": format(sum((Decimal(p["realized"]) for p in positions), Decimal(0)), "f"),
            "fees_and_taxes": format(
                sum((Decimal(p["fees_and_taxes"]) for p in positions), Decimal(0)), "f"
            ),
            "positions": positions,
        }


def view(account: PaperAccount) -> dict:
    orders = [PracticeOrder.model_validate(o) for o in account.orders]
    return {
        "id": account.id,
        "name": account.name,
        "initial_cash": format(account.initial_cash, "f"),
        "version": account.version,
        "orders": orders,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
        **balances(account.initial_cash, orders),
    }


async def owned(session, owner, account_id, *, lock=False):
    statement = select(PaperAccount).where(
        PaperAccount.id == account_id, PaperAccount.user_id == owner
    )
    if lock:
        statement = statement.with_for_update()
    account = await session.scalar(statement)
    if account is None:
        raise ServiceError("PRACTICE_NOT_FOUND", "找不到此模擬帳戶。", 404)
    return account


def version_matches(account, version):
    if account.version != version:
        raise ServiceError(
            "PRACTICE_VERSION_CONFLICT", "帳戶已被其他頁面修改，請保留輸入並重新載入帳戶。", 409
        )


class PracticeService:
    def __init__(self, factory: SessionFactory):
        self.factory = factory

    async def list(self, owner):
        async with self.factory() as session:
            await require_user(session, owner)
            rows = await session.scalars(
                select(PaperAccount)
                .where(PaperAccount.user_id == owner)
                .order_by(PaperAccount.created_at, PaperAccount.id)
            )
            return [view(account) for account in rows]

    async def get(self, owner, account_id):
        async with self.factory() as session:
            await require_user(session, owner)
            return view(await owned(session, owner, account_id))

    async def create(self, owner, body: AccountCreate):
        async with transaction(self.factory) as session:
            await require_user(session, owner, lock=True)
            count = await session.scalar(
                select(func.count()).select_from(PaperAccount).where(PaperAccount.user_id == owner)
            )
            if count is not None and count >= 5:
                raise ServiceError("PRACTICE_ACCOUNT_LIMIT", "每人最多 5 個模擬帳戶。", 409)
            account = PaperAccount(
                user_id=owner, name=body.name, initial_cash=body.initial_cash, orders=[]
            )
            session.add(account)
            await session.flush()
            return view(account)

    async def order(self, owner, account_id, body: OrderCreate):
        async with transaction(self.factory) as session:
            await require_user(session, owner, lock=True)
            account = await owned(session, owner, account_id, lock=True)
            orders = [PracticeOrder.model_validate(o) for o in account.orders]
            previous = next((o for o in orders if o.id == body.order.id), None)
            if previous:
                if previous.model_dump(exclude={"reflection"}) != body.order.model_dump(
                    exclude={"reflection"}
                ):
                    raise ServiceError(
                        "PRACTICE_ORDER_ID_CONFLICT", "相同交易識別碼不可提交不同內容。", 409
                    )
                return view(account)
            version_matches(account, body.version)
            if len(orders) >= 500:
                raise ServiceError("PRACTICE_ORDER_LIMIT", "每個模擬帳戶最多 500 筆練習。", 409)
            orders.append(body.order)
            if len({o.code for o in orders}) > 10:
                raise ServiceError(
                    "PRACTICE_INSTRUMENT_LIMIT", "每個模擬帳戶最多練習 10 檔證券。", 422
                )
            balances(account.initial_cash, orders)
            account.orders = [o.model_dump(mode="json") for o in orders]
            account.version += 1
            account.updated_at = await database_now(session)
            await session.flush()
            return view(account)

    async def reflect(self, owner, account_id, order_id, body: ReflectionUpdate):
        async with transaction(self.factory) as session:
            await require_user(session, owner, lock=True)
            account = await owned(session, owner, account_id, lock=True)
            version_matches(account, body.version)
            orders = [PracticeOrder.model_validate(o) for o in account.orders]
            target = next((o for o in orders if o.id == order_id), None)
            if target is None:
                raise ServiceError("PRACTICE_ORDER_NOT_FOUND", "找不到此練習交易。", 404)
            target.reflection = body.reflection.strip()
            account.orders = [o.model_dump(mode="json") for o in orders]
            account.version += 1
            account.updated_at = await database_now(session)
            await session.flush()
            return view(account)

    async def delete(self, owner, account_id, version):
        async with transaction(self.factory) as session:
            await require_user(session, owner, lock=True)
            account = await owned(session, owner, account_id, lock=True)
            version_matches(account, version)
            await session.delete(account)

    async def progress(self, owner):
        async with self.factory() as session:
            await require_user(session, owner)
            observed = await session.scalar(
                select(func.count())
                .select_from(WatchlistItem)
                .join(Watchlist)
                .where(Watchlist.user_id == owner)
            )
            notes = await session.scalar(
                select(func.count())
                .select_from(WorkspaceItem)
                .where(WorkspaceItem.user_id == owner, WorkspaceItem.kind == "note")
            )
            accounts = await session.scalars(
                select(PaperAccount).where(PaperAccount.user_id == owner)
            )
            orders = [o for a in accounts for o in a.orders]
            return {
                "observe": bool(observed),
                "note": bool(notes),
                "paper_buy": any(o.get("side") == "buy" for o in orders),
                "reflection": any(str(o.get("reflection") or "").strip() for o in orders),
            }
