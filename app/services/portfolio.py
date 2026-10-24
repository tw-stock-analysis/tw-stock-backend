"""Moving-average cost ledger. Market valuations are returned, never stored."""

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, localcontext

from app.schemas.instruments import InstrumentRef
from app.schemas.portfolio import LedgerEntry
from app.services.errors import ServiceError
from app.services.finmind import FinMindService
from app.services.instruments import InstrumentService
from app.services.research_transforms import price_rows


def ledger_positions(ledger: list[LedgerEntry]) -> list[dict]:
    positions: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "quantity": Decimal(0),
            "cost": Decimal(0),
            "realized": Decimal(0),
            "dividends": Decimal(0),
            "fees_and_taxes": Decimal(0),
        }
    )
    with localcontext() as context:
        context.prec = 64
        for _, item in sorted(enumerate(ledger), key=lambda pair: (pair[1].date, pair[0])):
            target = positions[(item.market, item.code)]
            charge = item.fees + item.taxes
            target["fees_and_taxes"] += charge
            if item.kind == "buy":
                assert item.quantity is not None and item.price is not None
                target["quantity"] += item.quantity
                target["cost"] += item.quantity * item.price + charge
            elif item.kind == "sell":
                assert item.quantity is not None and item.price is not None
                if item.quantity > target["quantity"]:
                    raise ServiceError(
                        "PORTFOLIO_OVERSELL", "賣出股數超過當時持有數量，請檢查交易順序。", 422
                    )
                removed = target["cost"] * item.quantity / target["quantity"]
                target["realized"] += item.quantity * item.price - charge - removed
                target["cost"] -= removed
                target["quantity"] -= item.quantity
                if target["quantity"] == 0:
                    target["cost"] = Decimal(0)
            elif item.kind == "dividend":
                target["dividends"] += item.amount or 0
            else:
                if target["quantity"] == 0:
                    raise ServiceError(
                        "PORTFOLIO_SPLIT_WITHOUT_HOLDING", "沒有持股時不能套用分割。", 422
                    )
                assert item.ratio is not None
                target["quantity"] *= item.ratio
        return [
            {
                "market": market,
                "code": code,
                **{key: format(value, "f") for key, value in values.items()},
            }
            for (market, code), values in sorted(positions.items())
        ]


async def _value_portfolio(ledger: list[LedgerEntry], source: FinMindService, as_of: date) -> dict:
    positions = ledger_positions([entry for entry in ledger if entry.date <= as_of])
    catalogue = (
        await InstrumentService(source).catalogue()
        if any(Decimal(p["quantity"]) > 0 for p in positions)
        else None
    )
    complete = True
    source_blocked = False
    with localcontext() as context:
        context.prec = 64
        for position in positions:
            quantity, cost = Decimal(position["quantity"]), Decimal(position["cost"])
            position.update(
                last_price=None, price_date=None, market_value=None, unrealized=None, error=None
            )
            if quantity == 0:
                position.update(market_value="0", unrealized="0")
                continue
            if source_blocked:
                complete = False
                position.update(
                    error="PORTFOLIO_NOT_REQUESTED",
                    error_message="來源存取受限，本次未繼續查詢此持股。",
                )
                continue
            try:
                assert catalogue is not None
                InstrumentService.resolve(
                    catalogue,
                    InstrumentRef(market=position["market"], code=position["code"]),
                    as_of,
                )
                start = as_of - timedelta(days=30)
                rows, meta = await source.fetch("TaiwanStockPrice", position["code"], start, as_of)
                points = [
                    r
                    for r in price_rows(rows, position["code"], start, as_of)
                    if r["close"] is not None
                ]
                if not points:
                    raise ServiceError(
                        "PORTFOLIO_PRICE_MISSING", "最近 30 天無可用收盤價，不推定市值。", 502
                    )
                last = points[-1]
                assert last["close"] is not None
                value = quantity * Decimal(last["close"])
                position.update(
                    last_price=last["close"],
                    price_date=last["date"],
                    market_value=format(value, "f"),
                    unrealized=format(value - cost, "f"),
                    retrieved_at=meta.retrieved_at.isoformat(),
                )
            except ServiceError as error:
                if error.code == "REQUEST_CANCELLED":
                    raise
                complete = False
                source_blocked = error.status_code in {403, 429, 503}
                position["error"] = error.code
                position["error_message"] = error.message
        realized = sum((Decimal(p["realized"]) for p in positions), Decimal(0))
        dividends = sum((Decimal(p["dividends"]) for p in positions), Decimal(0))
        unrealized = (
            sum((Decimal(p["unrealized"]) for p in positions), Decimal(0)) if complete else None
        )
        return {
            "positions": positions,
            "complete": complete,
            "as_of": as_of,
            "realized": format(realized, "f"),
            "dividends": format(dividends, "f"),
            "unrealized": format(unrealized, "f") if unrealized is not None else None,
            "total_pnl": format(realized + dividends + unrealized, "f")
            if unrealized is not None
            else None,
            "note": (
                "移動平均成本；分割與股利由使用者登錄。估值使用最近可用收盤價，"
                "非即時報價，不保存來源行情。"
            ),
        }


async def value_portfolio(ledger: list[LedgerEntry], source: FinMindService, as_of: date) -> dict:
    try:
        async with asyncio.timeout(50):
            return await _value_portfolio(ledger, source, as_of)
    except TimeoutError as error:
        raise ServiceError("RESEARCH_TIMEOUT", "本次估值查詢超時，請手動重試。", 503) from error
