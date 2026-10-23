"""Research uses fresh bounded snapshots; these rows never ship with the app."""

import asyncio
import csv
import io

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_twse_service
from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.services.twse import TwseService

DAILY_PATH = "/v1/exchangeReport/STOCK_DAY_ALL"
VALUE_PATH = "/v1/exchangeReport/BWIBBU_ALL"


def row(code, price="110", change="10", volume="100", day="1151022", **extra):
    return {
        "Code": code,
        "Name": f"測試{code}",
        "Date": day,
        "ClosingPrice": price,
        "Change": change,
        "TradeVolume": volume,
        **extra,
    }


@pytest.fixture
def research_api():
    app = create_app(Settings(_env_file=None, app_env="testing", postgres_password="test-only"))
    state = {
        "daily": [
            row("0050", volume="9007199254740993"),
            row("2330", "90", "-10"),
            row("2317", "100", "0"),
            row("8888", "--", "X0.00", None),
        ],
        "valuation": [
            {"Code": "2330", "Name": "測試2330", "Date": "1151021", "PEratio": "22.2200"}
        ],
        "failure": None,
        "calls": [],
        "active": 0,
        "valuation_delay": 0,
    }

    async def forbidden_db():
        raise AssertionError("Research must not open a database session")
        yield

    async def upstream(request):
        assert state["active"] == 0
        state["active"] += 1
        try:
            await asyncio.sleep(0)
            state["calls"].append(request.url.path)
            if request.url.path == VALUE_PATH:
                await asyncio.sleep(state["valuation_delay"])
            assert not request.url.query
            assert request.headers["cache-control"] == "no-cache"
            if state["failure"] == request.url.path:
                return httpx.Response(429)
            return httpx.Response(
                200, json=state["daily"] if request.url.path == DAILY_PATH else state["valuation"]
            )
        finally:
            state["active"] -= 1

    async def source():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            yield TwseService(client)

    app.dependency_overrides[get_session] = forbidden_db
    app.dependency_overrides[get_twse_service] = source
    with TestClient(app) as client:
        yield client, state


def test_screener_filters_whole_snapshot_before_sorting_and_pagination(research_api):
    client, state = research_api
    body = client.get(
        "/api/v1/research/screener?min_price=90&max_price=110&min_change=-10&max_change=10&sort=closing_price&order=desc&limit=1&page=2"
    ).json()
    assert body["pagination"] == {"page": 2, "limit": 1, "total": 3}
    assert body["data"][0]["code"] == "2317"
    assert body["data"][0]["change_percent"] == "0.0000"
    result = client.get("/api/v1/research/screener?min_volume=9007199254740993").json()
    assert [stock["code"] for stock in result["data"]] == ["0050"]
    assert result["data"][0]["trade_volume"] == "9007199254740993"
    assert result["data"][0]["change_percent"] == "10.0000"
    assert result["meta"]["data_date"] == "1151022"
    assert state["calls"] == [DAILY_PATH, DAILY_PATH]


@pytest.mark.parametrize(
    "order,expected",
    [("asc", ["2330", "2317", "0050", "8888"]), ("desc", ["0050", "2317", "2330", "8888"])],
)
def test_missing_values_are_last_in_both_sort_directions(research_api, order, expected):
    client, _ = research_api
    data = client.get(f"/api/v1/research/screener?sort=change_percent&order={order}").json()["data"]
    assert [stock["code"] for stock in data] == expected
    assert data[-1]["change_percent"] is None


def test_decimal_sort_and_filters_do_not_round_or_use_floating_point(research_api):
    client, state = research_api
    state["daily"] = [
        row("0001", volume="9007199254740992"),
        row("0002", volume="9007199254740993"),
        row("0003", "1.0300", "0.03", "1,234"),
    ]
    data = client.get("/api/v1/research/screener?sort=trade_volume&order=desc").json()["data"]
    assert [stock["code"] for stock in data] == ["0002", "0001", "0003"]
    data = client.get("/api/v1/research/screener?min_change=2.999999&max_change=3").json()["data"]
    assert data[0]["closing_price"] == "1.0300"
    assert data[0]["change_percent"] == "3.0000"


@pytest.mark.parametrize(
    "price,change",
    [("0", "0"), ("10", "10"), ("--", "1"), ("100", "X0.00"), ("NaN", "0"), ("Infinity", "1")],
)
def test_unavailable_or_invalid_reference_prices_do_not_invent_percentages(
    research_api, price, change
):
    client, state = research_api
    state["daily"] = [row("0050", price, change)]
    assert client.get("/api/v1/research/screener").json()["data"][0]["change_percent"] is None
    assert client.get("/api/v1/research/screener?min_change=-100").json()["data"] == []


@pytest.mark.parametrize(
    "path",
    [
        "screener?min_price=100&max_price=50",
        "screener?min_change=1&max_change=-1",
        "screener?min_price=NaN",
        "screener?min_price=Infinity",
        "screener?min_volume=-1",
        "screener?min_volume=1.5",
        "screener?sort=name",
        "screener?order=random",
        "screener?page=0",
        "screener?limit=101",
        "screener?unknown=x",
        "screener/export?min_price=-1",
        "compare?codes=0050",
        "compare?codes=0050,0050",
        "compare?codes=0050,2330,2317,8888,9999",
        "compare?codes=0050,invalid",
        "overview?date=1151022",
    ],
)
def test_invalid_inputs_are_rejected_before_source_requests(research_api, path):
    client, state = research_api
    response = client.get("/api/v1/research/" + path)
    assert response.status_code == 422
    assert not state["calls"]


def test_comparison_fetches_two_bulk_sources_in_order_and_preserves_dates(research_api):
    client, state = research_api
    body = client.get("/api/v1/research/compare?codes=2330,0050,9999").json()
    assert state["calls"] == [DAILY_PATH, VALUE_PATH]
    assert [item["code"] for item in body["data"]] == ["2330", "0050", "9999"]
    assert body["data"][0]["valuation"]["pe_ratio"] == "22.2200"
    assert body["data"][0]["quote"]["date"] == "1151022"
    assert body["data"][0]["valuation"]["date"] == "1151021"
    assert body["data"][1]["valuation"] is None
    assert body["data"][2]["quote"] is None
    assert body["valuation_error"] is None


def test_comparison_optional_failure_clears_estimates_and_quotes_never_fallback(research_api):
    client, state = research_api
    path = "/api/v1/research/compare?codes=2330,0050"
    assert client.get(path).json()["data"][0]["valuation"]
    state["failure"] = VALUE_PATH
    body = client.get(path).json()
    assert all(item["valuation"] is None for item in body["data"])
    assert body["data"][0]["quote"]
    assert body["valuation_error"]
    assert body["valuation_meta"] is None
    state["failure"] = DAILY_PATH
    response = client.get(path)
    assert response.status_code == 503
    assert "data" not in response.json()
    assert state["calls"] == [DAILY_PATH, VALUE_PATH, DAILY_PATH, VALUE_PATH, DAILY_PATH]


def test_unknown_comparison_skips_unneeded_valuation(research_api):
    client, state = research_api
    body = client.get("/api/v1/research/compare?codes=9998,9999").json()
    assert all(item["quote"] is None for item in body["data"])
    assert state["calls"] == [DAILY_PATH]


def test_comparison_bounds_optional_source_time_and_preserves_fresh_quote(
    research_api, monkeypatch
):
    client, state = research_api
    monkeypatch.setattr("app.services.research.VALUATION_TIMEOUT", 0.01)
    state["valuation_delay"] = 0.1
    body = client.get("/api/v1/research/compare?codes=0050,2330").json()
    assert body["data"][0]["quote"]["code"] == "0050"
    assert body["valuation_error"] == "估值來源逾時，請稍後重新取得。"
    assert body["valuation_meta"] is None
    assert all(item["valuation"] is None for item in body["data"])
    assert state["calls"] == [DAILY_PATH, VALUE_PATH]


def test_overview_excludes_stale_dates_and_distinguishes_missing_data(research_api):
    client, state = research_api
    state["daily"] += [
        row("0001", "100", "50", day="1151021"),
        row("0002", day=None),
        row("0003", day="1151332"),
    ]
    body = client.get("/api/v1/research/overview").json()
    assert [
        body[key]
        for key in ("total", "rising", "declining", "unchanged", "unclassified", "excluded")
    ] == [4, 1, 1, 1, 1, 3]
    assert [stock["code"] for stock in body["gainers"]] == ["0050"]
    assert [stock["code"] for stock in body["losers"]] == ["2330"]
    assert body["volume_leaders"][0]["code"] == "0050"
    assert body["meta"]["data_date"] == "1151022"
    assert state["calls"] == [DAILY_PATH]


def test_empty_snapshot_produces_empty_views_without_database(research_api):
    client, state = research_api
    state["daily"] = []
    assert client.get("/api/v1/research/screener").json()["pagination"]["total"] == 0
    body = client.get("/api/v1/research/overview").json()
    assert body["total"] == 0
    assert body["gainers"] == body["losers"] == body["volume_leaders"] == []
    assert body["meta"]["data_date"] is None


def test_csv_exports_all_filtered_rows_in_order_with_excel_safe_identifiers(research_api):
    client, state = research_api
    state["daily"][0]["Name"] = '=HYPERLINK("evil"),測試\n第二行'
    response = client.get("/api/v1/research/screener/export?min_price=0&sort=code&limit=1&page=2")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    records = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(records) == 4  # Header + every filtered row, ignoring UI pagination.
    assert records[1][0] == "'0050"
    assert records[1][1] == '\'=HYPERLINK("evil"),測試\n第二行'
    assert records[1][6] == "9007199254740993"
    assert records[3][4] == "'-10"
    assert state["calls"] == [DAILY_PATH]


@pytest.mark.parametrize("path", ["screener", "screener/export", "overview"])
def test_repeated_research_requests_fetch_fresh_and_do_not_reuse_success(research_api, path):
    client, state = research_api
    url = "/api/v1/research/" + path
    assert client.get(url).status_code == client.get(url).status_code == 200
    state["failure"] = DAILY_PATH
    response = client.get(url)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SOURCE_RATE_LIMITED"
    assert state["calls"] == [DAILY_PATH] * 3
