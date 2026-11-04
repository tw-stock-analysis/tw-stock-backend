"""Independent portfolio accounting, signal timing, coverage and source boundaries."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.analysis import FinMindMeta
from app.schemas.portfolio_backtest import PortfolioBacktestQuery
from app.services.errors import ServiceError
from app.services.portfolio_backtest import portfolio_backtest, simulate_portfolio

A, B = "TWSE:2330", "TPEX:6488"
START = date(2025, 1, 2)
WARMUP = date(2024, 1, 1)


def query(**kwargs):
    return PortfolioBacktestQuery.model_validate(
        {
            "assets": [
                {"code": "2330", "weight": "0.5"},
                {"market": "TPEX", "code": "6488", "weight": "0.5"},
            ],
            "start_date": "2025-01-02",
            "end_date": "2025-01-03",
            "initial_cash": "1000",
            "commission_rate": "0",
            "sell_tax_rate": "0",
            "minimum_fee": "0",
            **kwargs,
        }
    )


def row(code, day, opening=100, close=None, **extra):
    close = opening if close is None else close
    return {
        "stock_id": code,
        "date": str(day),
        "open": str(opening),
        "close": str(close),
        "max": str(max(opening, close)),
        "min": str(min(opening, close)),
        **extra,
    }


def prices(days=None):
    days = days or [date(2024, 12, 31), START, date(2025, 1, 3)]
    return {A: [row("2330", day) for day in days], B: [row("6488", day) for day in days]}


def factor_query(**kwargs):
    return query(
        **{
            "strategy": "momentum",
            "assets": [{"code": "2330"}, {"market": "TPEX", "code": "6488"}],
            "lookback": 20,
            "top_n": 1,
            "start_date": "2025-01-22",
            "end_date": "2025-01-23",
            **kwargs,
        }
    )


def factor_prices():
    result = {A: [], B: []}
    for index in range(21):
        day = date(2025, 1, 1) + timedelta(days=index)
        result[A].append(row("2330", day, 10 + index))
        result[B].append(row("6488", day, 20))
    result[A] += [row("2330", "2025-01-22", 5, 1), row("2330", "2025-01-23", 1)]
    result[B] += [row("6488", "2025-01-22", 20, 1000), row("6488", "2025-01-23", 1000)]
    return result


@pytest.mark.parametrize(
    "change",
    [
        {
            "assets": [
                {"code": "2330", "weight": ".5"},
                {"market": "TPEX", "code": "2330", "weight": ".5"},
            ]
        },
        {"assets": [{"code": "2330"}, {"code": "2317"}]},
        {"assets": [{"code": "2330", "weight": ".6"}, {"code": "2317", "weight": ".6"}]},
        {"assets": [{"code": "2330", "weight": "0"}, {"code": "2317", "weight": "0"}]},
        {
            "assets": [
                {"market": "EMERGING", "code": "1260", "weight": ".5"},
                {"code": "2317", "weight": ".5"},
            ]
        },
        {"strategy": "momentum"},
        {"top_n": 3},
        {"slippage_rate": 1},
    ],
)
def test_query_rejects_ambiguous_universe_or_invalid_allocations(change):
    with pytest.raises(ValidationError):
        query(**change)


def test_equal_weights_reconcile_no_costs_and_leave_no_invented_profit():
    result = simulate_portfolio(query(), prices(), WARMUP)
    assert len(result["orders"]) == 2
    assert all(
        order["date"] == "2025-01-02" and order["quantity"] == "5" for order in result["orders"]
    )
    assert Decimal(result["summary"]["final_equity"]) == 1000
    assert Decimal(result["summary"]["turnover"]) == 1
    assert Decimal(result["summary"]["fees_and_taxes"]) == 0
    assert all(Decimal(point["cash"]) >= 0 for point in result["equity"])


def test_cash_is_scaled_proportionally_with_minimum_fees_and_rounding():
    result = simulate_portfolio(query(minimum_fee="20"), prices(), WARMUP)
    assert [order["quantity"] for order in result["orders"]] == ["4", "4"]
    assert Decimal(result["equity"][0]["cash"]) == 160
    assert Decimal(result["summary"]["final_equity"]) == 960
    assert Decimal(result["summary"]["fees_and_taxes"]) == 40
    assert Decimal(result["summary"]["max_drawdown_percent"]) == 4


def test_weights_below_one_keep_the_requested_cash_reserve():
    result = simulate_portfolio(
        query(
            assets=[
                {"code": "2330", "weight": ".2"},
                {"market": "TPEX", "code": "6488", "weight": ".3"},
            ]
        ),
        prices(),
        WARMUP,
    )
    assert Decimal(result["equity"][-1]["cash"]) == 500
    assert sorted(order["quantity"] for order in result["orders"]) == ["2", "3"]


def test_monthly_rebalance_sells_before_buying_and_preserves_accounting():
    days = [date(2024, 12, 31), START, date(2025, 1, 31), date(2025, 2, 3)]
    data = prices(days)
    data[A][-1] = row("2330", "2025-02-03", 200)
    result = simulate_portfolio(query(end_date="2025-02-03"), data, WARMUP)
    later = [order for order in result["orders"] if order["date"] == "2025-02-03"]
    assert [(order["side"], order["quantity"]) for order in later] == [("sell", "2"), ("buy", "2")]
    assert Decimal(result["summary"]["final_equity"]) == 1500
    assert Decimal(result["equity"][-1]["cash"]) == 200
    assert result["rebalances"][-1]["signal_date"] == "2025-01-31"


def test_quarterly_does_not_rebalance_in_intermediate_months():
    data = prices([date(2024, 12, 31), START, date(2025, 2, 3), date(2025, 4, 1)])
    result = simulate_portfolio(query(end_date="2025-04-01", rebalance="quarterly"), data, WARMUP)
    assert [item["execution_date"] for item in result["rebalances"]] == ["2025-01-02", "2025-04-01"]


def test_momentum_uses_prior_close_and_next_open_not_current_close():
    result = simulate_portfolio(factor_query(), factor_prices(), WARMUP)
    assert [(order["code"], order["price"], order["quantity"]) for order in result["orders"]] == [
        ("2330", "5", "200")
    ]
    event = result["rebalances"][0]
    assert event["signal_date"] == "2025-01-21" and event["execution_date"] == "2025-01-22"
    assert Decimal(event["scores"][A]) == 200 and Decimal(event["scores"][B]) == 0
    assert event["weights"][B] == "0"


def test_low_volatility_selects_constant_price_and_ties_are_deterministic():
    data = factor_prices()
    result = simulate_portfolio(factor_query(strategy="low_volatility"), data, WARMUP)
    assert result["orders"][0]["code"] == "6488"
    for index in range(21):
        data[A][index] = row("2330", date(2025, 1, 1) + timedelta(days=index), 20)
    result = simulate_portfolio(factor_query(strategy="low_volatility"), data, WARMUP)
    assert result["orders"][0]["code"] == "6488"  # TPEX sorts before TWSE.
    assert all(Decimal(score) == 0 for score in result["rebalances"][0]["scores"].values())


def test_warmup_shortage_never_silently_excludes_an_asset():
    data = factor_prices()
    data[B] = data[B][1:]
    with pytest.raises(ServiceError) as caught:
        simulate_portfolio(factor_query(), data, WARMUP)
    assert caught.value.code == "FACTOR_HISTORY_INSUFFICIENT"


@pytest.mark.parametrize(
    "case", ["start", "end", "interior", "all_zero", "adjusted_forward_fill", "missing_asset"]
)
def test_incomplete_prices_reject_partial_or_stale_simulations(case):
    data = prices([date(2024, 12, 31), START, date(2025, 1, 3), date(2025, 1, 6)])
    if case == "start":
        data[B].pop(1)
    elif case == "end":
        data[B].pop()
    elif case == "interior":
        data[B].pop(2)
    elif case == "all_zero":
        data[A][2] = row("2330", "2025-01-03", 0)
        data[B][2] = row("6488", "2025-01-03", 0)
    elif case == "adjusted_forward_fill":
        data[B][2]["_raw_price_available"] = "0"
    else:
        data.pop(B)
    with pytest.raises(ServiceError) as caught:
        simulate_portfolio(query(end_date="2025-01-06"), data, WARMUP)
    assert caught.value.code == "PORTFOLIO_PRICE_COVERAGE"


def test_stock_lots_never_create_fractional_or_unaffordable_positions():
    result = simulate_portfolio(query(lot_size=1000), prices(), WARMUP)
    assert result["orders"] == []
    assert Decimal(result["equity"][-1]["cash"]) == 1000


def test_maximum_drawdown_uses_portfolio_nav():
    data = prices()
    data[A][-1] = row("2330", "2025-01-03", 50)
    data[B][-1] = row("6488", "2025-01-03", 50)
    result = simulate_portfolio(query(), data, WARMUP)
    assert Decimal(result["summary"]["final_equity"]) == 500
    assert Decimal(result["summary"]["return_percent"]) == -50
    assert Decimal(result["summary"]["max_drawdown_percent"]) == 50


def test_sell_costs_may_not_borrow_to_complete_rebalance():
    data = prices([date(2024, 12, 31), START, date(2025, 2, 3)])
    data[A][-1] = row("2330", "2025-02-03", 1000)
    data[B][-1] = row("6488", "2025-02-03", 1)
    with pytest.raises(ServiceError) as caught:
        simulate_portfolio(
            query(end_date="2025-02-03", commission_rate="1", sell_tax_rate="1"), data, WARMUP
        )
    assert caught.value.code == "BACKTEST_COST_EXCEEDS_CASH"


class Source:
    def __init__(self):
        self.calls = []
        self.data = prices()
        self.adjusted = None
        self.split = []

    async def catalogue(self, dataset):
        self.calls.append((dataset, None))
        rows = (
            [
                {"stock_id": "2330", "stock_name": "測試A", "type": "twse", "date": "2025-01-03"},
                {"stock_id": "6488", "stock_name": "測試B", "type": "tpex", "date": "2025-01-03"},
            ]
            if dataset == "TaiwanStockInfo"
            else []
        )
        return rows, self.meta(dataset, len(rows))

    async def fetch(self, dataset, code, start, end):
        self.calls.append((dataset, code))
        if dataset == "TaiwanStockPriceAdj":
            if isinstance(self.adjusted, Exception):
                raise self.adjusted
            rows = self.adjusted["TWSE:2330" if code == "2330" else "TPEX:6488"]
        elif dataset == "TaiwanStockPrice":
            rows = self.data["TWSE:2330" if code == "2330" else "TPEX:6488"]
        elif dataset == "TaiwanStockSplitPrice":
            rows = self.split
        else:
            rows = []
        return rows, self.meta(dataset, len(rows))

    @staticmethod
    def meta(dataset, count):
        return FinMindMeta(
            dataset=dataset,
            retrieved_at=datetime.now(UTC),
            query_start=WARMUP,
            query_end=date(2025, 1, 3),
            row_count=count,
            units={},
        )


async def test_default_identity_policy_fails_before_any_source_request():
    source = Source()
    with pytest.raises(ServiceError) as caught:
        await portfolio_backtest(query(), source)
    assert caught.value.code == "SECURITY_HISTORY_UNVERIFIED" and source.calls == []


async def test_verified_orchestrator_checks_each_asset_range_and_keeps_calls_sequential():
    source = Source()
    verify = AsyncMock()
    result = await portfolio_backtest(query(), source, verify)
    assert verify.await_count == 2
    assert all(call.args[1:] == (START, date(2025, 1, 3)) for call in verify.await_args_list)
    assert source.calls[:2] == [("TaiwanStockInfo", None), ("TaiwanStockDelisting", None)]
    assert [code for _, code in source.calls[2:]] == ["2330"] * 5 + ["6488"] * 5
    assert result["identity_verified"] is True


async def test_explicit_unverified_mode_is_labeled_not_claimed_as_verified():
    result = await portfolio_backtest(query(allow_unverified_identity=True), Source())
    assert result["identity_verified"] is False and "未驗證" in result["notes"][0]


async def test_corporate_action_guard_rejects_real_target_only():
    source = Source()
    source.split = [{"stock_id": "2330", "date": "2025-01-02"}]
    with pytest.raises(ServiceError) as caught:
        await portfolio_backtest(query(allow_unverified_identity=True), source)
    assert caught.value.code == "BACKTEST_ADJUSTMENT_REQUIRED"
    assert ("TaiwanStockPrice", "2330") not in source.calls


async def test_adjusted_permission_failure_aborts_instead_of_falling_back_to_raw():
    source = Source()
    source.adjusted = ServiceError("FINMIND_ACCESS_DENIED", "權限不足", 503)
    with pytest.raises(ServiceError) as caught:
        await portfolio_backtest(
            query(basis="source_adjusted", allow_unverified_identity=True), source
        )
    assert caught.value.code == "FINMIND_ACCESS_DENIED"
    assert all(code != "6488" for _, code in source.calls)


async def test_adjusted_forward_fill_is_masked_by_missing_raw_execution_price():
    source = Source()
    source.adjusted = prices()
    source.data[B][-1] = row("6488", "2025-01-03", 0)
    with pytest.raises(ServiceError) as caught:
        await portfolio_backtest(
            query(basis="source_adjusted", allow_unverified_identity=True), source
        )
    assert caught.value.code == "PORTFOLIO_PRICE_COVERAGE"


async def test_verified_history_overrides_latest_name_conflict_without_merging_quotes():
    from uuid import UUID

    source = Source()
    original_catalogue = source.catalogue

    async def catalogue(dataset):
        rows, meta = await original_catalogue(dataset)
        if dataset == "TaiwanStockInfo":
            rows = rows + [
                {"stock_id": "2330", "stock_name": "舊名稱", "type": "twse", "date": "2024-01-01"}
            ]
        return rows, meta

    source.catalogue = catalogue
    identities = {
        "2330": UUID("00000000-0000-0000-0000-000000000001"),
        "6488": UUID("00000000-0000-0000-0000-000000000002"),
    }

    async def verify(ref, start, end):
        return identities[ref.code]

    result = await portfolio_backtest(query(), source, verify)
    assert result["identity_verified"] is True
    assert result["assets"][0]["security_id"] == str(identities["2330"])
    assert result["assets"][0]["name"] == "2330"  # Ambiguous display names are not invented.
    with pytest.raises(ServiceError) as error:
        await portfolio_backtest(query(allow_unverified_identity=True), source)
    assert error.value.code == "INSTRUMENT_IDENTITY_AMBIGUOUS"


async def test_verified_historical_code_missing_from_current_catalogue_still_works():
    source = Source()
    original_catalogue = source.catalogue

    async def catalogue(dataset):
        rows, meta = await original_catalogue(dataset)
        return [row for row in rows if row.get("stock_id") != "2330"], meta

    source.catalogue = catalogue
    result = await portfolio_backtest(query(), source, AsyncMock(return_value=None))
    assert result["assets"][0]["name"] == "2330"
    assert result["summary"]["final_equity"] == "1000"
    with pytest.raises(ServiceError) as error:
        await portfolio_backtest(query(allow_unverified_identity=True), source)
    assert error.value.code == "INSTRUMENT_NOT_FOUND"


async def test_verified_identity_failure_stops_before_price_or_catalogue_queries():
    source = Source()
    verify = AsyncMock(side_effect=ServiceError("SECURITY_HISTORY_UNVERIFIED", "無已確認區間", 422))
    with pytest.raises(ServiceError) as error:
        await portfolio_backtest(query(), source, verify)
    assert error.value.code == "SECURITY_HISTORY_UNVERIFIED" and source.calls == []


async def test_aliases_for_one_verified_security_are_not_counted_as_two_assets():
    from uuid import UUID

    source = Source()
    verify = AsyncMock(return_value=UUID("00000000-0000-0000-0000-000000000001"))
    with pytest.raises(ServiceError) as error:
        await portfolio_backtest(query(), source, verify)
    assert error.value.code == "PORTFOLIO_DUPLICATE_SECURITY" and source.calls == []


async def test_strict_factor_verifies_actual_needed_window_not_entire_fetch_warmup():
    from uuid import UUID

    source = Source()
    source.data = factor_prices()
    calls = []
    identities = {
        "2330": UUID("00000000-0000-0000-0000-000000000001"),
        "6488": UUID("00000000-0000-0000-0000-000000000002"),
    }

    async def verify(ref, start, end):
        calls.append((ref.code, start, end))
        assert start >= date(2025, 1, 1)  # Earlier fetched dates were never used in the score.
        return identities[ref.code]

    result = await portfolio_backtest(factor_query(), source, verify)
    assert len(calls) == 4
    assert [start for _, start, _ in calls] == [date(2025, 1, 22)] * 2 + [date(2025, 1, 1)] * 2
    assert (
        result["factor_history_start"] == "2025-01-01" and result["factor_history_verified"] is True
    )


async def test_verified_execution_range_cannot_use_unverified_predecessor_factor_prices():
    from uuid import UUID

    source = Source()
    source.data = factor_prices()

    async def verify(ref, start, end):
        if start < date(2025, 1, 22):
            raise ServiceError("SECURITY_HISTORY_UNVERIFIED", "暖機期間身分未知", 422)
        return UUID(
            "00000000-0000-0000-0000-000000000001"
            if ref.code == "2330"
            else "00000000-0000-0000-0000-000000000002"
        )

    with pytest.raises(ServiceError) as error:
        await portfolio_backtest(factor_query(), source, verify)
    assert error.value.code == "SECURITY_HISTORY_UNVERIFIED"


async def test_factor_rejects_a_changed_identity_between_initial_and_warmup_checks():
    from uuid import UUID

    source = Source()
    source.data = factor_prices()

    async def verify(ref, start, end):
        if start < date(2025, 1, 22):
            return UUID("00000000-0000-0000-0000-000000000003")
        return UUID(
            "00000000-0000-0000-0000-000000000001"
            if ref.code == "2330"
            else "00000000-0000-0000-0000-000000000002"
        )

    with pytest.raises(ServiceError) as error:
        await portfolio_backtest(factor_query(), source, verify)
    assert error.value.code == "SECURITY_HISTORY_UNVERIFIED"


async def test_explicit_reference_factor_mode_keeps_unknown_warmup_labeled():
    source = Source()
    source.data = factor_prices()
    verify = AsyncMock(side_effect=AssertionError("No hidden verification"))
    result = await portfolio_backtest(factor_query(allow_unverified_identity=True), source, verify)
    assert not result["identity_verified"] and not result["factor_history_verified"]
    assert result["factor_history_start"] == "2025-01-01"
    assert verify.await_count == 0
