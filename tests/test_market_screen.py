"""Whole-source snapshot contracts: bounds, dates, coverage and no per-stock fallback."""

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
from app.schemas.market_screen import MarketScreenQuery
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, decode_rows
from app.services.market_screen import market_screen


def metadata(dataset, day=date(2026, 9, 1)):
    return FinMindMeta(
        dataset=dataset,
        retrieved_at=datetime.now(UTC),
        query_start=day,
        query_end=day,
        row_count=0,
        units={},
    )


class Source:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.info = [
            {"stock_id": code, "stock_name": name, "type": kind, "date": "2026-10-30"}
            for code, name, kind in [
                ("2330", "甲", "twse"),
                ("6488", "乙", "tpex"),
                ("1260", "丙", "emerging"),
            ]
        ]

    async def catalogue(self, dataset):
        self.calls.append(dataset)
        return self.info if dataset == "TaiwanStockInfo" else [], metadata(dataset)

    async def snapshot(self, dataset, day):
        self.calls.append((dataset, day))
        value = self.rows.get((dataset, day), [])
        if isinstance(value, Exception):
            raise value
        return value, metadata(dataset, day)

    async def fetch(self, *args):
        raise AssertionError("Never bypass snapshot access with per-stock requests")


def query(**kwargs):
    return MarketScreenQuery(revenue_month="2026-08-01", report_date="2026-06-30", **kwargs)


def revenue(code, day, year, month, amount):
    return {
        "stock_id": code,
        "date": day,
        "revenue_year": str(year),
        "revenue_month": str(month),
        "revenue": str(amount),
    }


async def test_snapshot_params_are_single_day_without_data_id_and_never_cached():
    calls = []

    async def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(
            200, json={"status": 200, "data": [revenue("2330", "2026-09-01", 2026, 8, 100)]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FinMindService(client)
        _, meta = await service.snapshot("TaiwanStockMonthRevenue", date(2026, 9, 1))
        await service.snapshot("TaiwanStockMonthRevenue", date(2026, 9, 1))
        assert meta.request_scope == "market_snapshot"
        assert meta.query_start == meta.query_end == date(2026, 9, 1)
        with pytest.raises(ValueError):
            await service.snapshot("TaiwanStockInfo", date(2026, 9, 1))
    assert calls == [{"dataset": "TaiwanStockMonthRevenue", "start_date": "2026-09-01"}] * 2


async def test_snapshot_rejects_wrong_date_and_permission_error_does_not_fetch_again():
    for payload, expected in [
        (
            {"status": 200, "data": [revenue("2330", "2026-09-02", 2026, 8, 100)]},
            "FINMIND_DATA_INVALID",
        ),
        (
            {"status": 400, "msg": "Please update your user level", "data": []},
            "FINMIND_ACCESS_DENIED",
        ),
    ]:
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ServiceError) as caught:
                await FinMindService(client).snapshot("TaiwanStockMonthRevenue", date(2026, 9, 1))
            assert caught.value.code == expected
        assert len(calls) == 1


def test_snapshot_row_allowance_is_explicit_and_does_not_weaken_stock_row_limit():
    import json

    payload = json.dumps(
        {
            "status": 200,
            "data": [
                {"stock_id": "2330", "date": "2026-06-30", "type": str(i)} for i in range(15001)
            ],
        }
    ).encode()
    with pytest.raises(ServiceError):
        decode_rows(payload)
    assert len(decode_rows(payload, max_rows=20000)) == 15001
    with pytest.raises(ServiceError) as caught:
        decode_rows(payload, max_rows=10000)
    assert caught.value.code == "FINMIND_SNAPSHOT_TOO_LARGE"


async def test_revenue_period_mapping_whole_market_coverage_and_exact_thresholds():
    source = Source()
    source.rows[("TaiwanStockMonthRevenue", date(2026, 9, 1))] = [
        revenue("2330", "2026-09-01", 2026, 8, 120),
        revenue("6488", "2026-09-01", 2026, 8, 90),
    ]
    source.rows[("TaiwanStockMonthRevenue", date(2025, 9, 1))] = [
        revenue("2330", "2025-09-01", 2025, 8, 100),
        revenue("6488", "2025-09-01", 2025, 8, 100),
    ]
    result = await market_screen(
        query(
            markets=["TWSE", "TPEX", "EMERGING"],
            rules={"min_revenue_yoy": 0},
            result_filter="all",
            sort="revenue_yoy",
            descending=True,
        ),
        source,
    )
    assert result["coverage"] == {
        "universe": 3,
        "matched": 1,
        "not_matched": 1,
        "insufficient": 1,
        "errors": 0,
        "complete": False,
    }
    assert [r["code"] for r in result["rows"]] == ["2330", "6488", "1260"]
    assert [r["matches"] for r in result["rows"]] == [True, False, None]
    assert source.calls == [
        "TaiwanStockInfo",
        "TaiwanStockDelisting",
        ("TaiwanStockMonthRevenue", date(2026, 9, 1)),
        ("TaiwanStockMonthRevenue", date(2025, 9, 1)),
    ]
    assert Decimal(result["rows"][0]["metrics"]["revenue_yoy"]) == 20


async def test_missing_classification_and_filters_preserve_total_universe_count():
    source = Source()
    result = await market_screen(query(rules={"min_gross_margin": 0}), source)
    assert result["rows"] == [] and result["coverage"]["universe"] == 2
    result = await market_screen(
        query(rules={"min_gross_margin": 0}, result_filter="insufficient", limit=1, page=2), source
    )
    assert result["pagination"] == {"page": 2, "limit": 1, "total": 2}
    assert len(result["rows"]) == 1 and result["rows"][0]["matches"] is None


async def test_income_balance_ratios_use_report_period_and_unrounded_decimal():
    source = Source()
    source.rows[("TaiwanStockFinancialStatements", date(2026, 6, 30))] = [
        {"stock_id": "2330", "date": "2026-06-30", "type": kind, "value": value}
        for kind, value in [
            ("Revenue", "1000000"),
            ("GrossProfit", "399999.9"),
            ("OperatingIncome", "-100000"),
        ]
    ]
    source.rows[("TaiwanStockBalanceSheet", date(2026, 6, 30))] = [
        {"stock_id": "2330", "date": "2026-06-30", "type": kind, "value": value}
        for kind, value in [("TotalAssets", "400"), ("Liabilities", "100")]
    ]
    result = await market_screen(
        query(
            markets=["TWSE"],
            rules={"min_gross_margin": 40, "max_debt_ratio": 25},
            result_filter="all",
        ),
        source,
    )
    row = result["rows"][0]
    assert row["matches"] is False
    assert Decimal(row["metrics"]["gross_margin"]) == Decimal("39.99999")
    assert Decimal(row["metrics"]["operating_margin"]) == -10
    assert Decimal(row["metrics"]["debt_ratio"]) == 25
    assert len(source.calls) == 4


async def test_empty_catalogue_and_wrong_revenue_month_never_claim_complete():
    source = Source()
    source.info = []
    with pytest.raises(ServiceError) as error:
        await market_screen(query(), source)
    assert error.value.code == "MARKET_UNIVERSE_EMPTY"
    source = Source()
    source.rows[("TaiwanStockMonthRevenue", date(2026, 9, 1))] = [
        revenue("2330", "2026-09-01", 2026, 9, 100)
    ]
    with pytest.raises(ServiceError) as error:
        await market_screen(query(rules={"min_revenue_yoy": 0}), source)
    assert error.value.code == "FINMIND_DATA_INVALID"


async def test_permission_failure_stops_without_partial_results_or_candidate_fallback():
    source = Source()
    source.rows[("TaiwanStockMonthRevenue", date(2026, 9, 1))] = ServiceError(
        "FINMIND_ACCESS_DENIED", "來源權限不足", 503
    )
    with pytest.raises(ServiceError) as error:
        await market_screen(query(rules={"min_revenue_yoy": 0, "min_gross_margin": 0}), source)
    assert error.value.code == "FINMIND_ACCESS_DENIED"
    assert len(source.calls) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"revenue_month": "2026-08-02"},
        {"revenue_month": "2099-08-01"},
        {"report_date": "2026-06-01"},
        {"markets": ["TWSE", "TWSE"]},
        {"report_date": "2099-06-30"},
    ],
)
def test_invalid_scope_rejected(changes):
    base = {"revenue_month": "2026-08-01", "report_date": "2026-06-30", **changes}
    with pytest.raises(ValidationError):
        MarketScreenQuery(**base)


def test_http_readonly_endpoint_json_body_limit_and_no_store():
    app = create_app(Settings(_env_file=None, postgres_password="test"))
    source = Source()
    app.dependency_overrides[get_finmind_service] = lambda: source
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/research/market-screen", json=query().model_dump(mode="json")
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/api/v1/research/market-screen",
                headers={"Content-Type": "application/json"},
                content=b"x" * 16385,
            ).status_code
            == 413
        )
        assert client.post("/api/v1/research/market-screen", content="x").status_code == 415


async def test_negative_total_liabilities_are_not_a_favorable_debt_ratio():
    source = Source()
    source.rows[("TaiwanStockBalanceSheet", date(2026, 6, 30))] = [
        {"stock_id": "2330", "date": "2026-06-30", "type": kind, "value": value}
        for kind, value in [("TotalAssets", "100"), ("Liabilities", "-1")]
    ]
    result = await market_screen(
        query(markets=["TWSE"], rules={"max_debt_ratio": "10"}, result_filter="all"), source
    )
    assert result["rows"][0]["matches"] is None
    assert result["rows"][0]["metrics"]["debt_ratio"] is None


async def test_conflicting_security_name_does_not_display_merged_metrics():
    source = Source()
    source.info.append({**source.info[0], "stock_name": "不同發行人"})
    source.rows[("TaiwanStockMonthRevenue", date(2026, 9, 1))] = [
        revenue("2330", "2026-09-01", 2026, 8, 200)
    ]
    source.rows[("TaiwanStockMonthRevenue", date(2025, 9, 1))] = [
        revenue("2330", "2025-09-01", 2025, 8, 100)
    ]
    result = await market_screen(
        query(markets=["TWSE"], rules={"min_revenue_yoy": 0}, result_filter="all"), source
    )
    row = result["rows"][0]
    assert row["status"] == "error" and row["matches"] is None
    assert all(value is None for value in row["metrics"].values())


async def test_snapshot_byte_limit_fails_as_incomplete_without_a_second_request(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"status": 200, "data": [revenue("2330", "2026-09-01", 2026, 8, 100)]}
        )

    monkeypatch.setattr("app.services.finmind.SNAPSHOT_MAX_BYTES", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError) as error:
            await FinMindService(client).snapshot("TaiwanStockMonthRevenue", date(2026, 9, 1))
        assert error.value.code == "FINMIND_SNAPSHOT_TOO_LARGE"
    assert len(calls) == 1
