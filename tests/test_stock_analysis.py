"""Source contracts, calculation boundaries and no-persistence regression coverage."""

import asyncio
import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.deps import get_finmind_service, get_twse_service
from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.services.finmind import FinMindService
from app.services.twse import TwseService

CODE = "0050"
TOKEN = "test-only.secret.must-not-appear"
PATH = f"/api/v1/stocks/{CODE}/analysis?start_date=2026-07-01&end_date=2026-07-31"
DAYS = [date(2026, 4, 1) + timedelta(days=i) for i in range(122)]
DAYS = [day for day in DAYS if day.weekday() < 5]
PRICES = [
    {
        "date": day.isoformat(),
        "stock_id": CODE,
        "open": str(99 + i),
        "max": str(101 + i),
        "min": str(98 + i),
        "close": str(100 + i),
        "Trading_Volume": 1000,
        "Trading_money": "12345678901234567890.1234",
    }
    for i, day in enumerate(DAYS)
]
REVENUE = [
    {
        "date": recorded,
        "stock_id": CODE,
        "revenue_year": year,
        "revenue_month": month,
        "revenue": amount,
        "create_time": created,
    }
    for year, month, amount, recorded, created in (
        (2025, 5, 100, "2025-06-01", ""),
        (2025, 6, 0, "2025-07-01", ""),
        (2026, 4, 200, "2026-05-01", "2026-05-05"),
        (2026, 5, 250, "2026-06-01", None),
        (2026, 6, 0, "2026-07-01", "2026-07-09"),
    )
]
TRADES = [
    {"date": day.isoformat(), "stock_id": CODE, "name": name, "buy": buy, "sell": sell}
    for day in DAYS
    for name, buy, sell in (
        ("Foreign_Investor", 150, 50),
        ("Investment_Trust", 0, 0),
        ("Foreign_Dealer", 15, 10),
        ("Dealer_self", 20, 0),
        ("Dealer_Hedging", 0, 30),
    )
]
# Institutions have a shorter warm-up query than prices.
TRADES = [row for row in TRADES if row["date"] >= "2026-04-02"]
PAYLOADS = {
    "TaiwanStockPrice": PRICES,
    "TaiwanStockMonthRevenue": REVENUE,
    "TaiwanStockInstitutionalInvestorsBuySell": TRADES,
}


@pytest.fixture
def analysis_api():
    app = create_app(Settings(_env_file=None, postgres_password="test-only", app_env="testing"))
    calls = []
    replies = {}

    async def forbidden_db():
        raise AssertionError("Research must not open a database session")
        yield

    app.dependency_overrides[get_session] = forbidden_db

    def upstream(request):
        if request.url.host == "openapi.twse.com.tw":
            calls.append("TWSE")
            return httpx.Response(200, json=[{"Code": CODE, "Name": "測試 ETF"}])
        dataset = request.url.params["dataset"]
        calls.append(dataset)
        assert request.url.host == "api.finmindtrade.com"
        assert request.url.path == "/api/v4/data"
        assert request.url.params["data_id"] == CODE
        assert "token" not in request.url.params
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["cache-control"] == "no-cache"
        if dataset in replies:
            return replies[dataset]
        return httpx.Response(
            200, json={"status": 200, "msg": "success", "data": list(reversed(PAYLOADS[dataset]))}
        )

    async def twse_service():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            yield TwseService(client)

    async def finmind_service():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            yield FinMindService(client, TOKEN)

    app.dependency_overrides[get_twse_service] = twse_service
    app.dependency_overrides[get_finmind_service] = finmind_service
    with TestClient(app) as client:
        yield client, calls, replies


def test_sources_are_fresh_sequential_and_never_open_database(analysis_api):
    client, calls, _ = analysis_api
    for _ in range(2):
        response = client.get(PATH)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["complete"] is True
        assert TOKEN not in response.text
    assert calls == ["TWSE", *PAYLOADS] * 2


def test_price_warmup_precision_and_visible_range(analysis_api):
    client, _, _ = analysis_api
    result = client.get(PATH).json()["history"]
    first = result["data"][0]
    assert first["date"] == "2026-07-01"
    before = [Decimal(row["close"]) for row in PRICES if row["date"] <= first["date"]]
    assert Decimal(first["ma20"]) == sum(before[-20:]) / 20
    assert Decimal(first["ma60"]) == sum(before[-60:]) / 60
    assert first["amount"] == "12345678901234567890.1234"
    assert first["volume"] == "1000"
    assert result["meta"]["price_basis"] == "unadjusted"
    assert result["meta"]["units"]["volume"] == "share"


def test_revenue_uses_business_month_not_collection_date(analysis_api):
    client, _, _ = analysis_api
    rows = {p["month"]: p for p in client.get(PATH).json()["revenue"]["data"]}
    may = rows["2026-05"]
    assert may["revenue"] == "250"
    assert may["yoy"] == "150.0000"
    assert may["mom"] == "25.0000"
    assert may["record_date"] == "2026-06-01"
    assert may["recorded_at"] is None
    assert rows["2026-06"]["revenue"] == "0"
    assert rows["2026-06"]["yoy"] is None
    assert rows["2026-06"]["mom"] == "-100.0000"


def test_institutions_keep_categories_signed_shares_and_rolling_windows(analysis_api):
    client, _, _ = analysis_api
    first = client.get(PATH).json()["institutions"]["data"][0]
    trades = {row["name"]: row for row in first["trades"]}
    assert set(trades) == {
        "Foreign_Investor",
        "Foreign_Dealer",
        "Investment_Trust",
        "Dealer_self",
        "Dealer_Hedging",
    }
    assert trades["Foreign_Investor"]["net5"] == "500"
    assert trades["Foreign_Investor"]["net20"] == "2000"
    assert trades["Dealer_Hedging"]["net"] == "-30"
    assert trades["Investment_Trust"]["net"] == "0"
    assert trades["Foreign_Dealer"]["net"] == "5"
    assert "total" not in trades


def test_missing_history_or_category_never_fabricates_zero(analysis_api):
    client, _, replies = analysis_api
    replies["TaiwanStockPrice"] = httpx.Response(200, json={"status": 200, "data": PRICES[-1:]})
    rows = [row for row in TRADES if row["date"] >= "2026-07-01"]
    rows = [
        row
        for row in rows
        if not (row["date"] == "2026-07-02" and row["name"] == "Foreign_Investor")
    ]
    replies["TaiwanStockInstitutionalInvestorsBuySell"] = httpx.Response(
        200, json={"status": 200, "data": rows}
    )
    body = client.get(PATH).json()
    assert body["history"]["data"][0]["ma20"] is None
    assert body["history"]["data"][0]["ma60"] is None
    days = body["institutions"]["data"]
    assert not any(t["name"] == "Foreign_Investor" for t in days[1]["trades"])
    investor = next(t for t in days[4]["trades"] if t["name"] == "Foreign_Investor")
    assert investor["net5"] is None


@pytest.mark.parametrize(
    "query",
    [
        "start_date=2026-07-31&end_date=2026-07-01",
        "start_date=2020-01-01&end_date=2026-07-31",
        "start_date=1990-01-01&end_date=1990-02-01",
        "end_date=2999-01-01",
        "start_date=2026-02-30",
        "url=https://example.com",
    ],
)
def test_invalid_queries_make_no_source_calls(analysis_api, query):
    client, calls, _ = analysis_api
    assert client.get(f"/api/v1/stocks/{CODE}/analysis?{query}").status_code == 422
    assert calls == []


def test_unknown_or_invalid_codes_never_reach_finmind(analysis_api):
    client, calls, _ = analysis_api
    assert client.get(PATH.replace(CODE, "999999")).status_code == 404
    assert calls == ["TWSE"]
    calls.clear()
    assert client.get(PATH.replace(CODE, "bad-code")).status_code == 422
    assert not calls


@pytest.mark.parametrize("status", [401, 402, 403, 429, 503])
def test_global_source_failures_stop_remaining_datasets_without_retry(analysis_api, status):
    client, calls, replies = analysis_api
    replies["TaiwanStockPrice"] = httpx.Response(status, json={"msg": TOKEN})
    body = client.get(PATH).json()
    assert body["complete"] is False
    assert body["history"]["status"] == "error"
    assert body["revenue"]["status"] == body["institutions"]["status"] == "not_requested"
    assert body["history"]["data"] is None
    assert calls == ["TWSE", "TaiwanStockPrice"]
    assert TOKEN not in json.dumps(body)


def test_business_quota_error_is_not_treated_as_empty_data(analysis_api):
    client, calls, replies = analysis_api
    replies["TaiwanStockPrice"] = httpx.Response(
        200, json={"status": 402, "data": [], "msg": TOKEN}
    )
    body = client.get(PATH).json()
    assert body["history"]["error"]["code"] == "FINMIND_RATE_LIMITED"
    assert calls == ["TWSE", "TaiwanStockPrice"]


@pytest.mark.parametrize(
    "problem",
    ["duplicate", "wrong_code", "wrong_date", "bad_number", "bad_range", "nested", "tiny_exponent"],
)
def test_invalid_prices_are_isolated_and_other_datasets_still_load(analysis_api, problem):
    client, calls, replies = analysis_api
    rows = deepcopy(PRICES)
    if problem == "duplicate":
        rows.append(rows[0])
    elif problem == "wrong_code":
        rows[0]["stock_id"] = "2330"
    elif problem == "wrong_date":
        rows[0]["date"] = "1900-01-01"
    elif problem == "bad_number":
        rows[0]["close"] = "NaN"
    elif problem == "bad_range":
        rows[0]["min"] = "999"
    elif problem == "nested":
        rows[0]["other"] = {"value": 1}
    else:
        rows[0]["close"] = "1e-999999"
    replies["TaiwanStockPrice"] = httpx.Response(200, json={"status": 200, "data": rows})
    body = client.get(PATH).json()
    assert body["history"]["error"]["code"] == "FINMIND_DATA_INVALID"
    assert body["history"]["data"] is None
    assert body["revenue"]["status"] == body["institutions"]["status"] == "ok"
    assert calls == ["TWSE", *PAYLOADS]


def test_requery_after_success_returns_no_previous_values(analysis_api):
    client, _, replies = analysis_api
    assert client.get(PATH).json()["history"]["data"]
    replies["TaiwanStockPrice"] = httpx.Response(503)
    body = client.get(PATH).json()
    assert body["history"]["data"] is None
    assert body["revenue"]["data"] is None


def test_successful_empty_data_is_distinct_from_failed_data(analysis_api):
    client, _, replies = analysis_api
    for dataset in PAYLOADS:
        replies[dataset] = httpx.Response(200, json={"status": 200, "data": []})
    body = client.get(PATH).json()
    assert body["complete"] is True
    assert body["history"]["data"] == []
    assert body["history"]["meta"]["row_count"] == 0


def test_prelisting_opening_reference_is_flagged_not_drawn_as_standard_open(analysis_api):
    client, _, replies = analysis_api
    rows = deepcopy(PRICES[-1:])
    rows[0]["open"] = "999"
    replies["TaiwanStockPrice"] = httpx.Response(200, json={"status": 200, "data": rows})
    point = client.get(PATH).json()["history"]["data"][0]
    assert point["open"] == "999"
    assert point["opening_reference"] is True


@pytest.mark.asyncio
async def test_shared_finmind_service_serializes_calls_and_allows_anonymous_requests():
    active = maximum = 0
    calls = []

    async def upstream(request):
        nonlocal active, maximum
        assert "authorization" not in request.headers
        calls.append(request)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, json={"status": 200, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = FinMindService(client)
        await asyncio.gather(
            *(
                service.fetch("TaiwanStockPrice", CODE, date(2026, 7, 1), date(2026, 7, 31))
                for _ in range(5)
            )
        )
    assert len(calls) == 5
    assert maximum == 1


def test_token_is_masked_in_settings():
    settings = Settings(_env_file=None, finmind_api_token=TOKEN)
    assert TOKEN not in repr(settings)
    assert TOKEN not in settings.model_dump_json()


def test_disconnect_stops_subsequent_source_requests(analysis_api, monkeypatch):
    client, calls, _ = analysis_api
    checks = 0

    async def disconnected(self):
        nonlocal checks
        checks += 1
        return checks >= 3

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    response = client.get(PATH)
    assert response.status_code == 499
    assert response.json()["error"]["code"] == "REQUEST_CANCELLED"
    assert calls == ["TWSE", "TaiwanStockPrice"]


def test_oversized_source_is_bounded_and_does_not_expose_source_body(analysis_api, monkeypatch):
    from app.services import finmind

    client, _, _ = analysis_api
    monkeypatch.setattr(finmind, "MAX_BYTES", 100)
    body = client.get(PATH).json()
    assert body["history"]["error"]["code"] == "FINMIND_DATA_INVALID"
    assert body["history"]["data"] is None
    assert TOKEN not in json.dumps(body)
