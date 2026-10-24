"""Single-security next-open simulation, with explicit costs and corporate-action guards."""

import asyncio
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal, localcontext

from app.schemas.lab import BacktestQuery
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, Row, invalid_data
from app.services.instruments import InstrumentService
from app.services.research_transforms import action_dates, checked, event_rows
from app.services.stock_analysis import number


def simulate(query: BacktestQuery, rows: list[Row], start: date) -> dict:
    assert query.start_date is not None
    bars = []
    for row in checked(rows, query.instrument.code, start, query.end_date):
        values = [number(row, key) for key in ("open", "max", "min", "close")]
        opening, high, low, close = values
        if any(value <= 0 for value in values):
            continue
        if not low <= opening <= high or not low <= close <= high:
            raise invalid_data()
        bars.append((row["date"], opening, close))
    bars.sort()
    cash, quantity, costs = query.initial_cash, Decimal(0), Decimal(0)
    closes: list[Decimal] = []
    orders, equity = [], []
    peak, drawdown = cash, Decimal(0)
    signal = False
    with localcontext() as context:
        context.prec = 64
        for day, opening, close in bars:
            assert day is not None
            active = day >= query.start_date.isoformat()
            # This signal was computed from previous bars only.
            target = query.strategy == "buy_hold" or signal
            if active and target and quantity == 0:
                price = opening * (1 + query.slippage_rate)
                lot_cost = price * query.lot_size
                units = min(
                    cash / (lot_cost * (1 + query.commission_rate)),
                    max(Decimal(0), cash - query.minimum_fee) / lot_cost,
                ).to_integral_value(rounding=ROUND_FLOOR)
                shares = units * query.lot_size
                if shares > 0:
                    fee = max(query.minimum_fee, shares * price * query.commission_rate)
                    cash -= shares * price + fee
                    quantity, costs = shares, costs + fee
                    orders.append(
                        {
                            "date": day,
                            "side": "buy",
                            "quantity": str(shares),
                            "price": str(price),
                            "fee": str(fee),
                            "tax": "0",
                        }
                    )
            elif active and not target and quantity > 0:
                price = opening * (1 - query.slippage_rate)
                fee = max(query.minimum_fee, quantity * price * query.commission_rate)
                tax = quantity * price * query.sell_tax_rate
                cash += quantity * price - fee - tax
                if cash < 0:
                    raise ServiceError(
                        "BACKTEST_COST_EXCEEDS_CASH", "設定費稅造成現金不足，請檢查交易成本。", 422
                    )
                costs += fee + tax
                orders.append(
                    {
                        "date": day,
                        "side": "sell",
                        "quantity": str(quantity),
                        "price": str(price),
                        "fee": str(fee),
                        "tax": str(tax),
                    }
                )
                quantity = Decimal(0)
            if active:
                value = cash + quantity * close
                peak = max(peak, value)
                drawdown = max(drawdown, (peak - value) / peak)
                equity.append(
                    {
                        "date": day,
                        "equity": str(value),
                        "cash": str(cash),
                        "quantity": str(quantity),
                        "close": str(close),
                    }
                )
            closes.append(close)
            signal = (
                len(closes) >= query.slow
                and sum(closes[-query.fast :]) / query.fast
                > sum(closes[-query.slow :]) / query.slow
            )
        if not equity:
            raise ServiceError("BACKTEST_NO_PRICES", "區間內無有效可成交日線。", 422)
        return {
            "orders": orders,
            "equity": equity,
            "final_equity": equity[-1]["equity"],
            "return_percent": str((Decimal(equity[-1]["equity"]) / query.initial_cash - 1) * 100),
            "max_drawdown_percent": str(drawdown * 100),
            "fees_and_taxes": str(costs),
            "basis": query.basis,
            "notes": [
                "單一證券模擬；前一有效日收盤產生訊號，次一有效日開盤成交；均線不足時不交易。",
                "費率、滑價、最低手續費與整股／零股單位由使用者設定；不模擬漲跌停排隊、流動性或成交失敗，期末持股按收盤估值，不強制賣出。",
                "原始股價模式不含股利與再投資；還原價格使用來源調整後單位，非實際歷史成交價。",
                "名錄與價格為目前來源查得資料，無完整永久證券身分、已知資訊時點或全市場存活者偏誤修正。",
            ],
        }


async def backtest(query: BacktestQuery, source: FinMindService) -> dict:
    async with asyncio.timeout(50):
        catalogue = await InstrumentService(source).catalogue()
        item = InstrumentService.resolve(catalogue, query.instrument, query.end_date)
        assert query.start_date is not None
        start = max(date(1994, 10, 1), query.start_date - timedelta(days=365))
        sources = []
        if query.basis == "unadjusted":
            for dataset in (
                "TaiwanStockSplitPrice",
                "TaiwanStockCapitalReductionReferencePrice",
                "TaiwanStockParValueChange",
                "TaiwanStockDividendResult",
            ):
                actions, meta = await source.fetch(
                    dataset, query.instrument.code, start, query.end_date
                )
                sources.append(meta)
                relevant = action_dates(actions, query.instrument.code, start, query.end_date)
                if dataset == "TaiwanStockDividendResult":
                    relevant = [
                        r
                        for r in event_rows(actions, query.instrument.code, start, query.end_date)
                        if r.get("kind") not in ("息", "除息")
                    ]
                if relevant:
                    raise ServiceError(
                        "BACKTEST_ADJUSTMENT_REQUIRED",
                        "區間含分割、減資、面額變更或非純現金除權息，請使用具權限的還原價格模式。",
                        422,
                    )
        rows, meta = await source.fetch(
            "TaiwanStockPriceAdj" if query.basis == "source_adjusted" else "TaiwanStockPrice",
            query.instrument.code,
            start,
            query.end_date,
        )
        sources.append(meta)
        return {
            "instrument": item,
            "parameters": query,
            "sources": sources,
            **simulate(query, rows, start),
        }
