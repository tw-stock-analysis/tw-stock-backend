"""Source adapters, partial screening and independent accounting/backtest invariants."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import get_finmind_service
from app.core.config import Settings
from app.main import create_app
from app.schemas.analysis import FinMindMeta
from app.schemas.instruments import InstrumentRef, InstrumentSearch
from app.schemas.lab import BacktestQuery, FactorQuery
from app.schemas.portfolio import LedgerEntry
from app.schemas.workspace import WorkspaceCreate
from app.services.backtest import backtest, simulate
from app.services.errors import ServiceError
from app.services.factors import screen
from app.services.finmind import FinMindService
from app.services.instruments import InstrumentService
from app.services.portfolio import ledger_positions, value_portfolio

DAY = date(2025, 3, 31)


def meta(dataset, count=0):
    return FinMindMeta(
        dataset=dataset,
        retrieved_at=datetime.now(UTC),
        query_start=date(1994, 10, 1),
        query_end=DAY,
        row_count=count,
        units={},
    )


class Source:
    def __init__(self):
        self.calls = []
        self.data = {}
        self.info = [
            dict(stock_id=code, stock_name="證券" + code, type=market, date="2025-03-31")
            for code, market in [("2330", "twse"), ("6488", "tpex"), ("4578", "emerging")]
        ]
        self.removed = [dict(stock_id="9999", stock_name="舊證券", date="2020-01-02")]

    async def catalogue(self, dataset):
        self.calls.append(dataset)
        rows = self.info if dataset == "TaiwanStockInfo" else self.removed
        return rows, meta(dataset, len(rows))

    async def fetch(self, dataset, code, start, end):
        self.calls.append((dataset, code))
        rows = self.data.get(dataset, [])
        if isinstance(rows, Exception):
            raise rows
        return rows, meta(dataset, len(rows))


async def test_catalogue_handles_unknown_dates_historical_markets_and_ambiguous_codes():
    source = Source()
    source.info += [
        dict(stock_id="8888", stock_name="未知日期", type="tpex", date="None"),
        dict(stock_id="7777", stock_name="同名", type="twse", date="2024-01-01"),
        dict(stock_id="7777", stock_name="同名", type="tpex", date="2025-01-01"),
        dict(stock_id="6666", stock_name="更名前", type="twse", date="2024-01-01"),
        dict(stock_id="6666", stock_name="更名後", type="twse", date="2025-01-01"),
    ]
    service = InstrumentService(source)
    catalogue = await service.catalogue()
    assert source.calls == ["TaiwanStockInfo", "TaiwanStockDelisting"]
    assert (
        service.resolve(catalogue, InstrumentRef(market="TPEX", code="8888"), DAY).reference_date
        is None
    )
    assert (
        service.resolve(catalogue, InstrumentRef(market="TWSE", code="7777"), DAY).market == "TPEX"
    )
    with pytest.raises(ServiceError, match="衝突"):
        service.resolve(catalogue, InstrumentRef(code="6666"), DAY)
    result = await service.search(InstrumentSearch(market="DELISTED"))
    assert [r.code for r in result.data] == ["9999"]
    assert result.data[0].delisted_on == date(2020, 1, 2)


async def test_catalogue_requests_are_allowlisted_uncached_and_do_not_send_stock_dates():
    calls = []

    async def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={"status": 200, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = FinMindService(client)
        _, catalogue_meta = await source.catalogue("TaiwanStockInfo")
        assert catalogue_meta.query_start is None and catalogue_meta.query_end is None
        assert catalogue_meta.request_scope == "catalogue"
        await source.catalogue("TaiwanStockInfo")
        with pytest.raises(ValueError):
            await source.catalogue("unknown")
    assert calls == [{"dataset": "TaiwanStockInfo"}] * 2


async def test_screen_only_queries_selected_metrics_and_never_matches_missing_values():
    source = Source()
    query = FactorQuery(
        instruments=[{"code": "2330"}, {"market": "TPEX", "code": "6488"}],
        as_of=DAY,
        rules={"min_revenue_yoy": "0"},
    )
    result = await screen(query, source)
    assert [r["status"] for r in result["rows"]] == ["insufficient"] * 2
    assert all(r["matches"] is None for r in result["rows"])
    assert source.calls == [
        "TaiwanStockInfo",
        "TaiwanStockDelisting",
        ("TaiwanStockMonthRevenue", "2330"),
        ("TaiwanStockMonthRevenue", "6488"),
    ]


async def test_screen_stops_after_quota_and_preserves_unrequested_candidates():
    source = Source()
    source.data["TaiwanStockMonthRevenue"] = ServiceError("FINMIND_RATE_LIMITED", "限額", 429)
    result = await screen(
        FactorQuery(
            instruments=[{"code": "2330"}, {"market": "TPEX", "code": "6488"}],
            as_of=DAY,
            rules={"min_revenue_yoy": 0},
        ),
        source,
    )
    assert {r["code"]: r["status"] for r in result["rows"]} == {
        "2330": "error",
        "6488": "not_requested",
    }
    assert len(source.calls) == 3


async def test_screen_ratios_use_latest_period_and_signed_decimal_values():
    source = Source()
    source.data["TaiwanStockFinancialStatements"] = [
        {"stock_id": "2330", "date": "2024-12-31", "type": k, "value": v, "origin_name": k}
        for k, v in [("Revenue", "200"), ("GrossProfit", "80"), ("OperatingIncome", "-20")]
    ]
    source.data["TaiwanStockBalanceSheet"] = [
        {"stock_id": "2330", "date": "2024-12-31", "type": k, "value": v, "origin_name": k}
        for k, v in [("TotalAssets", "500"), ("Liabilities", "125")]
    ]
    result = await screen(
        FactorQuery(
            instruments=[{"code": "2330"}],
            as_of=DAY,
            rules={
                "min_gross_margin": "39.99",
                "min_operating_margin": "-11",
                "max_debt_ratio": "25",
            },
        ),
        source,
    )
    row = result["rows"][0]
    assert row["matches"] is True
    assert Decimal(row["metrics"]["gross_margin"]) == 40
    assert Decimal(row["metrics"]["operating_margin"]) == -10
    assert Decimal(row["metrics"]["debt_ratio"]) == 25


def entry(kind, **kwargs):
    return LedgerEntry(code="2330", kind=kind, date="2025-01-02", **kwargs)


def test_ledger_cost_realized_dividends_and_split_reconcile():
    result = ledger_positions(
        [
            entry("buy", quantity="10", price="100", fees="10"),
            entry("split", ratio="2"),
            entry("sell", quantity="5", price="60", fees="2", taxes="3"),
            entry("dividend", amount="30"),
        ]
    )[0]
    assert Decimal(result["quantity"]) == 15
    assert Decimal(result["cost"]) == Decimal("757.5")
    assert Decimal(result["realized"]) == Decimal("42.5")
    assert Decimal(result["dividends"]) == 30
    assert Decimal(result["fees_and_taxes"]) == 15


def test_ledger_rejects_historical_oversell_even_when_later_buy_covers_it():
    ledger = [
        entry("sell", quantity="1", price="100"),
        LedgerEntry(code="2330", kind="buy", date="2025-01-03", quantity="2", price="100"),
    ]
    with pytest.raises(ServiceError) as error:
        ledger_positions(ledger)
    assert error.value.code == "PORTFOLIO_OVERSELL"


async def test_missing_portfolio_quote_does_not_invent_zero_or_total_pnl():
    result = await value_portfolio([entry("buy", quantity="10", price="100")], Source(), DAY)
    assert result["complete"] is False
    assert result["positions"][0]["market_value"] is None
    assert result["total_pnl"] is None
    assert result["positions"][0]["error"] == "PORTFOLIO_PRICE_MISSING"


def bars(prices):
    return [
        dict(
            stock_id="2330",
            date=f"2025-01-{day:02}",
            open=str(opening),
            max=str(max(opening, close)),
            min=str(min(opening, close)),
            close=str(close),
        )
        for day, opening, close in prices
    ]


def test_sma_signal_executes_at_next_open_and_does_not_use_current_close():
    query = BacktestQuery(
        instrument={"code": "2330"},
        start_date="2025-01-04",
        end_date="2025-01-07",
        fast=2,
        slow=3,
        initial_cash="1000",
        commission_rate="0",
        minimum_fee="0",
        sell_tax_rate="0",
    )
    rows = bars(
        [(1, 10, 10), (2, 10, 10), (3, 20, 20), (4, 12, 1), (5, 8, 1), (6, 9, 20), (7, 13, 20)]
    )
    result = simulate(query, rows, date(2024, 1, 1))
    assert [(r["date"], r["side"], r["price"]) for r in result["orders"]] == [
        ("2025-01-04", "buy", "12"),
        ("2025-01-06", "sell", "9"),
        ("2025-01-07", "buy", "13"),
    ]
    assert Decimal(result["equity"][0]["equity"]) == Decimal("87")  # 83 shares + 4 cash


def test_buy_hold_budget_includes_minimum_fee_and_lot_constraint():
    query = BacktestQuery(
        instrument={"code": "2330"},
        strategy="buy_hold",
        start_date="2025-01-02",
        end_date="2025-01-03",
        initial_cash="1000",
        minimum_fee="20",
        commission_rate="0",
        lot_size=1,
    )
    result = simulate(query, bars([(2, 100, 100), (3, 100, 110)]), date(2024, 1, 1))
    assert Decimal(result["orders"][0]["quantity"]) == 9
    assert Decimal(result["final_equity"]) == 1070
    assert Decimal(result["equity"][0]["cash"]) == 80
    assert len(result["orders"]) == 1


async def test_backtest_corporate_action_guard_filters_other_instruments_and_rejects_split():
    source = Source()
    source.data["TaiwanStockSplitPrice"] = [{"stock_id": "6488", "date": "2025-01-02"}]
    source.data["TaiwanStockPrice"] = bars([(2, 100, 100)])
    query = BacktestQuery(
        instrument={"code": "2330"},
        strategy="buy_hold",
        start_date="2025-01-02",
        end_date="2025-01-02",
    )
    assert (await backtest(query, source))["orders"]
    source.data["TaiwanStockSplitPrice"].append({"stock_id": "2330", "date": "2025-01-02"})
    with pytest.raises(ServiceError) as error:
        await backtest(query, source)
    assert error.value.code == "BACKTEST_ADJUSTMENT_REQUIRED"


@pytest.mark.parametrize(
    "document",
    [
        {
            "kind": "note",
            "name": "研究",
            "instrument": {"code": "2330"},
            "body": "內容",
            "prices": [],
        },
        {"kind": "comparison", "name": "重複", "instruments": [{"code": "2330"}, {"code": "2330"}]},
        {"kind": "portfolio", "name": "行情", "ledger": [], "market_results": {}},
    ],
)
def test_documents_reject_untyped_market_results_and_duplicates(document):
    with pytest.raises(ValidationError):
        WorkspaceCreate(document=document)


def test_http_mutations_require_origin_csrf_and_real_byte_limit_but_public_research_has_no_auth():
    app = create_app(
        Settings(
            _env_file=None, postgres_password="test", auth_allowed_origins=["https://app.test"]
        )
    )
    source = Source()
    app.dependency_overrides[get_finmind_service] = lambda: source
    with TestClient(app, base_url="https://app.test") as client:
        for method, path in [
            ("POST", "/api/v1/workspace/items"),
            ("PUT", "/api/v1/workspace/items/00000000-0000-0000-0000-000000000000"),
            ("DELETE", "/api/v1/workspace/items/00000000-0000-0000-0000-000000000000?version=1"),
        ]:
            assert (
                client.request(
                    method, path, headers={"Content-Type": "application/json"}, json={}
                ).status_code
                == 403
            )
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://app.test",
            "X-CSRF-Protection": "1",
        }
        assert (
            client.post(
                "/api/v1/workspace/items", headers=headers, content=b"x" * 131073
            ).status_code
            == 413
        )
        response = client.post(
            "/api/v1/research/factors",
            json={"instruments": [{"code": "2330"}], "as_of": "2025-03-31"},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/api/v1/research/factors",
                content=b"x" * 16385,
                headers={"Content-Type": "application/json"},
            ).status_code
            == 413
        )
        assert client.get("/api/v1/workspace/items").status_code == 401
        detail = client.get("/api/v1/instruments/TPEX/6488?end_date=2025-03-31")
        assert detail.status_code == 200
        assert detail.json()["instrument"]["market"] == "TPEX"


async def test_screen_compares_unrounded_ratios_at_threshold():
    source = Source()
    source.data["TaiwanStockFinancialStatements"] = [
        {
            "stock_id": "2330",
            "date": "2024-12-31",
            "type": subject,
            "value": value,
            "origin_name": subject,
        }
        for subject, value in [("Revenue", "1000000"), ("GrossProfit", "399999.9")]
    ]
    result = await screen(
        FactorQuery(instruments=[{"code": "2330"}], as_of=DAY, rules={"min_gross_margin": "40"}),
        source,
    )
    row = result["rows"][0]
    assert row["matches"] is False
    assert Decimal(row["metrics"]["gross_margin"]) == Decimal("39.99999")


async def test_request_source_cancellation_stops_before_another_provider_call():
    from app.services.request_source import RequestSource

    source = Source()
    checks = iter([False, True])

    async def disconnected():
        return next(checks)

    guarded = RequestSource(source, disconnected)
    with pytest.raises(ServiceError) as error:
        await InstrumentService(guarded).catalogue()
    assert error.value.code == "REQUEST_CANCELLED"
    assert source.calls == ["TaiwanStockInfo"]


async def test_screen_deadline_leaves_incomplete_candidates_unclassified(monkeypatch):
    import asyncio

    original = asyncio.timeout
    monkeypatch.setattr("app.services.factors.asyncio.timeout", lambda seconds: original(0.005))
    source = Source()

    async def blocked(*args):
        await asyncio.Event().wait()

    source.fetch = blocked
    result = await screen(
        FactorQuery(
            instruments=[{"code": "2330"}, {"market": "TPEX", "code": "6488"}],
            as_of=DAY,
            rules={"min_revenue_yoy": 0},
        ),
        source,
    )
    assert not result["complete"]
    assert all(
        row["status"] == "not_requested" and row["matches"] is None for row in result["rows"]
    )


def test_backtest_rejects_costs_that_would_require_borrowing():
    query = BacktestQuery(
        instrument={"code": "2330"},
        start_date="2025-01-04",
        end_date="2025-01-05",
        fast=2,
        slow=3,
        initial_cash="1000",
        minimum_fee="0",
        commission_rate="1",
        sell_tax_rate="1",
    )
    with pytest.raises(ServiceError) as error:
        simulate(
            query,
            bars([(1, 10, 10), (2, 20, 20), (3, 30, 30), (4, 10, 1), (5, 10, 1)]),
            date(2024, 1, 1),
        )
    assert error.value.code == "BACKTEST_COST_EXCEEDS_CASH"


async def test_portfolio_stops_remaining_quotes_after_provider_quota():
    source = Source()
    source.data["TaiwanStockPrice"] = ServiceError("FINMIND_RATE_LIMITED", "來源額度不足", 429)
    result = await value_portfolio(
        [
            entry("buy", quantity="1", price="100"),
            LedgerEntry(
                market="TPEX", code="6488", kind="buy", date="2025-01-02", quantity="1", price="100"
            ),
        ],
        source,
        DAY,
    )
    by_code = {r["code"]: r for r in result["positions"]}
    # Stable ledger ordering is market then code: TPEX is queried before TWSE.
    assert by_code["6488"]["error"] == "FINMIND_RATE_LIMITED"
    assert by_code["2330"]["error"] == "PORTFOLIO_NOT_REQUESTED"
    assert sum(isinstance(call, tuple) for call in source.calls) == 1
    assert result["total_pnl"] is None
