"""Request-local, next-open portfolio allocation and cross-sectional price factors."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal, localcontext
from uuid import UUID

from app.schemas.instruments import InstrumentRef
from app.schemas.portfolio_backtest import PortfolioBacktestAsset, PortfolioBacktestQuery
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, Row, invalid_data
from app.services.instruments import InstrumentService
from app.services.research_transforms import action_dates, checked, event_rows
from app.services.stock_analysis import number, record_date

IdentityVerifier = Callable[[InstrumentRef, date, date], Awaitable[object]]
Bar = tuple[Decimal, Decimal]
ZERO = Decimal(0)
ONE = Decimal(1)


def asset_key(asset: PortfolioBacktestAsset) -> str:
    return f"{asset.market}:{asset.code}"


def failure(code: str, message: str) -> ServiceError:
    return ServiceError(code, message, 422)


def valid_bar(row: Row) -> Bar | None:
    opening, high, low, close = [number(row, key) for key in ("open", "max", "min", "close")]
    if high < low or (close > 0 and not low <= close <= high):
        raise invalid_data()
    if (
        min(opening, high, low, close) <= 0
        or row.get("_raw_price_available") == "0"
        or ("Trading_Volume" in row and number(row, "Trading_Volume", integer=True) == 0)
    ):
        return None
    if not low <= opening <= high:
        raise invalid_data()
    return opening, close


def _calendar(
    query: PortfolioBacktestQuery, percodebars: dict[str, list[Row]], warmup_start: date
) -> tuple[dict[str, dict[date, Bar]], list[date], list[date]]:
    assert query.start_date is not None
    keys = {asset_key(asset) for asset in query.assets}
    if set(percodebars) != keys:
        raise failure(
            "PORTFOLIO_PRICE_COVERAGE", "每個指定標的都需要價格，不可省略或混入其他標的。"
        )
    all_days: set[date] = set()
    bars: dict[str, dict[date, Bar]] = {}
    for asset in query.assets:
        key = asset_key(asset)
        series: dict[date, Bar] = {}
        for row in checked(percodebars[key], asset.code, warmup_start, query.end_date):
            day = record_date(row)
            all_days.add(day)
            point = valid_bar(row)
            if point is not None:
                series[day] = point
        bars[key] = series
    active = sorted(day for day in all_days if query.start_date <= day <= query.end_date)
    if (
        not active
        or active[0] != query.start_date
        or active[-1] != query.end_date
        or any(day not in series for day in active for series in bars.values())
    ):
        raise failure(
            "PORTFOLIO_PRICE_COVERAGE",
            "起訖日期及區間內各來源交易日須有所有標的的有效日線；請選擇共同交易日，缺值或停牌時不沿用舊價。",
        )
    common = sorted(set.intersection(*(set(series) for series in bars.values())))
    if not any(day < query.start_date for day in common):
        raise failure(
            "FACTOR_HISTORY_INSUFFICIENT", "開始日前至少需要一筆所有標的共同有效收盤資料。"
        )
    return bars, active, common


def _weights(
    query: PortfolioBacktestQuery,
    bars: dict[str, dict[date, Bar]],
    common: list[date],
    execution_date: date,
) -> tuple[date, dict[str, Decimal], dict[str, Decimal]]:
    history = [day for day in common if day < execution_date]
    signal_date = history[-1]
    keys = sorted(bars)
    if query.strategy == "fixed_weights":
        return (
            signal_date,
            {asset_key(asset): asset.weight or ZERO for asset in query.assets},
            {},
        )
    if len(history) < query.lookback + 1:
        raise failure(
            "FACTOR_HISTORY_INSUFFICIENT",
            "所有標的共同有效收盤筆數不足因子暖機需求；不排除缺資料標的或縮短觀察期。",
        )
    window = history[-query.lookback - 1 :]
    scores = {}
    for key in keys:
        closes = [bars[key][day][1] for day in window]
        if query.strategy == "momentum":
            scores[key] = (closes[-1] / closes[0] - ONE) * 100
        else:
            returns = [right / left - ONE for left, right in zip(closes, closes[1:])]
            mean = sum(returns, ZERO) / len(returns)
            variance = sum(((value - mean) ** 2 for value in returns), ZERO) / len(returns)
            scores[key] = variance.sqrt() * Decimal(252).sqrt() * 100
    ranked = sorted(
        keys,
        key=lambda key: (
            -scores[key] if query.strategy == "momentum" else scores[key],
            key,
        ),
    )
    chosen = set(ranked[: query.top_n])
    weight = ONE / query.top_n
    # Repeating fractions can round infinitesimally above one. The final chosen
    # asset receives the residual so the exact Decimal allocation never borrows.
    weights = {key: ZERO for key in keys}
    selected = sorted(chosen)
    for key in selected[:-1]:
        weights[key] = weight
    weights[selected[-1]] = ONE - weight * (len(selected) - 1)
    return signal_date, weights, scores


def _fee(query: PortfolioBacktestQuery, quantity: Decimal, price: Decimal) -> Decimal:
    return max(query.minimum_fee, quantity * price * query.commission_rate) if quantity else ZERO


def _affordable_buys(
    query: PortfolioBacktestQuery,
    requested: dict[str, Decimal],
    prices: dict[str, Decimal],
    cash: Decimal,
) -> tuple[dict[str, Decimal], Decimal]:
    def scaled(scale: Decimal) -> dict[str, Decimal]:
        return {
            key: (quantity / query.lot_size * scale).to_integral_value(rounding=ROUND_FLOOR)
            * query.lot_size
            for key, quantity in requested.items()
        }

    def cost(quantities: dict[str, Decimal]) -> Decimal:
        return sum(
            (
                quantity * prices[key] + _fee(query, quantity, prices[key])
                for key, quantity in quantities.items()
            ),
            ZERO,
        )

    if cost(requested) <= cash:
        return requested, ONE
    low, high = ZERO, ONE
    for _ in range(100):
        midpoint = (low + high) / 2
        if cost(scaled(midpoint)) <= cash:
            low = midpoint
        else:
            high = midpoint
    quantities = scaled(low)
    if cost(quantities) > cash:
        raise failure("BACKTEST_COST_EXCEEDS_CASH", "設定成本超過可用現金，無法進行不借款的配置。")
    return quantities, low


def simulate_portfolio(
    query: PortfolioBacktestQuery, percodebars: dict[str, list[Row]], warmup_start: date
) -> dict:
    """All signals use completed common bars; only execution OPEN sizes the order."""
    bars, calendar, common = _calendar(query, percodebars, warmup_start)
    assets = {asset_key(asset): asset for asset in query.assets}
    keys = sorted(assets)
    quantities = dict.fromkeys(keys, ZERO)
    cash, charges, gross_traded = query.initial_cash, ZERO, ZERO
    peak, maximum_drawdown = cash, ZERO
    orders: list[dict] = []
    equity: list[dict] = []
    rebalances: list[dict] = []
    previous_period: tuple[int, int] | None = None
    with localcontext() as context:
        context.prec = 64
        for day in calendar:
            period = (day.year, day.month if query.rebalance == "monthly" else (day.month - 1) // 3)
            if period != previous_period:
                signal_date, weights, scores = _weights(query, bars, common, day)
                openings = {key: bars[key][day][0] for key in keys}
                # Current OPEN is the execution price, never a signal or a use of
                # the current CLOSE. Target dollars refer to opening account NAV.
                opening_equity = cash + sum((quantities[key] * openings[key] for key in keys), ZERO)
                targets = {
                    key: (
                        opening_equity * weights[key] / openings[key] / query.lot_size
                    ).to_integral_value(rounding=ROUND_FLOOR)
                    * query.lot_size
                    for key in keys
                }
                for key in keys:
                    quantity = max(ZERO, quantities[key] - targets[key])
                    if not quantity:
                        continue
                    price = openings[key] * (ONE - query.slippage_rate)
                    gross = quantity * price
                    fee, tax = _fee(query, quantity, price), gross * query.sell_tax_rate
                    cash += gross - fee - tax
                    if cash < 0:
                        raise failure(
                            "BACKTEST_COST_EXCEEDS_CASH", "賣出費稅造成現金不足，不自動借款。"
                        )
                    quantities[key] -= quantity
                    charges += fee + tax
                    gross_traded += gross
                    orders.append(
                        {
                            "date": day.isoformat(),
                            "market": assets[key].market,
                            "code": assets[key].code,
                            "side": "sell",
                            "quantity": str(quantity),
                            "price": str(price),
                            "gross": str(gross),
                            "fee": str(fee),
                            "tax": str(tax),
                        }
                    )
                requested = {key: max(ZERO, targets[key] - quantities[key]) for key in keys}
                buy_prices = {key: openings[key] * (ONE + query.slippage_rate) for key in keys}
                purchases, scale = _affordable_buys(query, requested, buy_prices, cash)
                for key in keys:
                    quantity = purchases[key]
                    if not quantity:
                        continue
                    price = buy_prices[key]
                    gross, fee = quantity * price, _fee(query, quantity, price)
                    cash -= gross + fee
                    if cash < 0:
                        raise failure(
                            "BACKTEST_COST_EXCEEDS_CASH", "買進費用造成現金不足，不自動借款。"
                        )
                    quantities[key] += quantity
                    charges += fee
                    gross_traded += gross
                    orders.append(
                        {
                            "date": day.isoformat(),
                            "market": assets[key].market,
                            "code": assets[key].code,
                            "side": "buy",
                            "quantity": str(quantity),
                            "price": str(price),
                            "gross": str(gross),
                            "fee": str(fee),
                            "tax": "0",
                        }
                    )
                rebalances.append(
                    {
                        "signal_date": signal_date.isoformat(),
                        "execution_date": day.isoformat(),
                        "weights": {key: str(weights[key]) for key in keys},
                        "scores": {key: str(value) for key, value in scores.items()},
                        "cash_scaling": str(scale),
                    }
                )
                previous_period = period
            positions = [
                {
                    "market": assets[key].market,
                    "code": assets[key].code,
                    "quantity": str(quantities[key]),
                    "close": str(bars[key][day][1]),
                    "market_value": str(quantities[key] * bars[key][day][1]),
                }
                for key in keys
            ]
            value = cash + sum((quantities[key] * bars[key][day][1] for key in keys), ZERO)
            peak = max(peak, value)
            maximum_drawdown = max(maximum_drawdown, (peak - value) / peak)
            equity.append(
                {
                    "date": day.isoformat(),
                    "equity": str(value),
                    "cash": str(cash),
                    "positions": positions,
                }
            )
        final = Decimal(equity[-1]["equity"])
        turnover = gross_traded / query.initial_cash
        return {
            "parameters": query.model_dump(mode="json"),
            "assets": [asset.model_dump(mode="json") for asset in query.assets],
            "orders": orders,
            "equity": equity,
            "rebalances": rebalances,
            "summary": {
                "final_equity": str(final),
                "return_percent": str((final / query.initial_cash - ONE) * 100),
                "max_drawdown_percent": str(maximum_drawdown * 100),
                "fees_and_taxes": str(charges),
                "turnover": str(turnover),
                "turnover_percent": str(turnover * 100),
            },
            "notes": [
                "固定的使用者指定標的集合，不是歷史全市場；即使證券身分已驗證，也未消除標的挑選與存活者偏誤。",
                "每月／季第一個區間內共同交易日調整；因子只使用成交日前共同有效收盤資料，當日開盤只作成交與配置股數換算。因子暖機身分另見回應的驗證範圍標示。",
                (
                    "動能為 lookback 筆共同有效日期的收盤價變動百分比；"
                    "低波動採同窗每日報酬母體標準差乘根號252，非基本面公告時點回測。"
                ),
                "先賣後買，費用不足時各買進股數依同一比例縮放並向下取交易單位；剩餘現金保留，無借款、放空或期末強制清倉。",
                "周轉比為雙邊累計成交金額除以初始資金；費率、最低費用與滑價由使用者設定，不模擬漲跌停排隊、成交量限制或券商逐筆進位。",
                "原始價格模式不含現金股利或再投資；還原價格和股數為來源調整單位，非歷史真實成交價與實際股數。",
            ],
        }


async def portfolio_backtest(
    query: PortfolioBacktestQuery,
    source: FinMindService,
    verify_identity: IdentityVerifier | None = None,
) -> dict:
    assert query.start_date is not None
    if not query.allow_unverified_identity and verify_identity is None:
        raise failure(
            "SECURITY_HISTORY_UNVERIFIED",
            "請先建立可驗證的歷史證券身分，或明確選擇未驗證身分研究模式。",
        )
    try:
        async with asyncio.timeout(50):
            verified_ids: dict[str, str | None] = {}
            if not query.allow_unverified_identity:
                assert verify_identity is not None
                for asset in query.assets:
                    verified = await verify_identity(
                        InstrumentRef(market=asset.market, code=asset.code),
                        query.start_date,
                        query.end_date,
                    )
                    verified_ids[asset_key(asset)] = (
                        str(verified) if isinstance(verified, UUID) else None
                    )
                known_ids = [value for value in verified_ids.values() if value is not None]
                if len(set(known_ids)) != len(known_ids):
                    raise failure(
                        "PORTFOLIO_DUPLICATE_SECURITY",
                        "多個代碼指向同一永久證券，不重複配置同一身分。",
                    )
            catalogue = await InstrumentService(source).catalogue()
            names: dict[str, str] = {}
            for asset in query.assets:
                key = asset_key(asset)
                if query.allow_unverified_identity:
                    # Without historical evidence retain the conservative reference
                    # resolver, including its rename/code-reuse ambiguity rejection.
                    reference = InstrumentService.resolve(
                        catalogue,
                        InstrumentRef(market=asset.market, code=asset.code),
                        query.end_date,
                    )
                    names[key] = reference.name
                else:
                    # Verified historical ranges are authoritative. A current
                    # catalogue cannot veto a known old code or a proven rename.
                    label_reference = next(
                        (item for item in catalogue.data if item.code == asset.code), None
                    )
                    names[key] = (
                        label_reference.name
                        if label_reference is not None
                        and label_reference.historical_identity != "ambiguous"
                        else asset.code
                    )
            warmup_start = max(
                date(1994, 10, 1),
                query.start_date - timedelta(days=max(365, query.lookback * 2 + 60)),
            )
            percodebars: dict[str, list[Row]] = {}
            sources = list(catalogue.sources)
            for asset in query.assets:
                if query.basis == "unadjusted":
                    for dataset in (
                        "TaiwanStockSplitPrice",
                        "TaiwanStockCapitalReductionReferencePrice",
                        "TaiwanStockParValueChange",
                        "TaiwanStockDividendResult",
                    ):
                        actions, meta = await source.fetch(
                            dataset, asset.code, warmup_start, query.end_date
                        )
                        sources.append(meta)
                        relevant = action_dates(actions, asset.code, warmup_start, query.end_date)
                        if dataset == "TaiwanStockDividendResult":
                            relevant = [
                                row
                                for row in event_rows(
                                    actions, asset.code, warmup_start, query.end_date
                                )
                                if row.get("kind") not in ("息", "除息")
                            ]
                        if relevant:
                            raise failure(
                                "BACKTEST_ADJUSTMENT_REQUIRED",
                                "標的區間含分割、減資、面額變更或非純現金除權息，請使用具權限的還原價格模式。",
                            )
                raw, raw_meta = await source.fetch(
                    "TaiwanStockPrice", asset.code, warmup_start, query.end_date
                )
                sources.append(raw_meta)
                if query.basis == "source_adjusted":
                    adjusted, adjusted_meta = await source.fetch(
                        "TaiwanStockPriceAdj", asset.code, warmup_start, query.end_date
                    )
                    sources.append(adjusted_meta)
                    raw_by_day = {
                        record_date(row): valid_bar(row)
                        for row in checked(raw, asset.code, warmup_start, query.end_date)
                    }
                    # Adjusted data can forward-fill a price for a raw no-price day.
                    # Those positive synthetic prices must never become executable.
                    percodebars[asset_key(asset)] = [
                        {
                            **row,
                            "_raw_price_available": "1"
                            if raw_by_day.get(record_date(row))
                            else "0",
                        }
                        for row in checked(adjusted, asset.code, warmup_start, query.end_date)
                    ]
                else:
                    percodebars[asset_key(asset)] = raw
            factor_history_start: date | None = None
            if query.strategy != "fixed_weights":
                _, _, common = _calendar(query, percodebars, warmup_start)
                prior = [day for day in common if day < query.start_date]
                if len(prior) < query.lookback + 1:
                    raise failure(
                        "FACTOR_HISTORY_INSUFFICIENT", "所有標的共同有效收盤筆數不足因子暖機需求。"
                    )
                factor_history_start = prior[-query.lookback - 1]
                if not query.allow_unverified_identity:
                    assert verify_identity is not None
                    for asset in query.assets:
                        verified = await verify_identity(
                            InstrumentRef(market=asset.market, code=asset.code),
                            factor_history_start,
                            query.end_date,
                        )
                        initial_id = verified_ids[asset_key(asset)]
                        if initial_id is not None and (
                            not isinstance(verified, UUID) or str(verified) != initial_id
                        ):
                            raise failure(
                                "SECURITY_HISTORY_UNVERIFIED",
                                "因子暖機與交易期間未確認為同一永久證券，停止使用可能屬於前身的價格。",
                            )
            result = simulate_portfolio(query, percodebars, warmup_start)
            result["assets"] = [
                {
                    **asset.model_dump(mode="json"),
                    "name": names[asset_key(asset)],
                    "security_id": verified_ids.get(asset_key(asset)),
                }
                for asset in query.assets
            ]
            result["sources"] = [meta.model_dump(mode="json") for meta in sources]
            result["identity_verified"] = not query.allow_unverified_identity
            result["factor_history_start"] = (
                factor_history_start.isoformat() if factor_history_start else None
            )
            result["factor_history_verified"] = (
                (not query.allow_unverified_identity) if factor_history_start else None
            )
            if factor_history_start and not query.allow_unverified_identity:
                result["notes"].insert(
                    0,
                    "因子實際使用的暖機區間與交易區間均驗證為同一永久證券，不使用身分未知的前身價格。",
                )
            if not query.allow_unverified_identity:
                result["notes"].insert(
                    0,
                    (
                        "證券身分依已驗證永久 ID 與交易期間；"
                        "顯示名稱若來自目前名錄，不代表該交易日名稱。"
                    ),
                )
            if query.allow_unverified_identity:
                result["notes"].insert(
                    0, "本次明確使用未驗證身分研究模式；代碼重用、轉板及上市前歷史可能使結果失真。"
                )
            return result
    except TimeoutError as error:
        raise ServiceError(
            "RESEARCH_TIMEOUT", "本次跨股回測來源查詢超時，請縮小標的或期間後手動重試。", 503
        ) from error
