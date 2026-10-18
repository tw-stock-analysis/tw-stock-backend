"""Synthetic stock contracts. Every successful call still needs a fresh TWSE request."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_twse_service
from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.services.twse import TwseService

DAILY = [
    {
        "Date": "1151016",
        "Code": "0050",
        "Name": "測試ETF",
        "ClosingPrice": "123.4500",
        "Change": "0",
        "TradeVolume": "12345678901234567890",
        "OpeningPrice": None,
    },
    {"Date": "1151016", "Code": "2330", "Name": "測試股票", "ClosingPrice": "0", "Change": "-1.00"},
]
VALUATION = [
    {
        "Date": "1151015",
        "Code": "2330",
        "Name": "測試股票",
        "PEratio": "20.0000",
        "DividendYield": "",
        "PBratio": "-",
    }
]
MARGIN = [
    {
        "股票代號": "2330",
        "股票名稱": "測試股票",
        "融資買進": "12",
        "融資今日餘額": "0",
        "融券今日餘額": None,
        "註記": "來源原始註記",
    }
]
SOURCE_PATHS = {
    "/v1/exchangeReport/STOCK_DAY_ALL": DAILY,
    "/v1/exchangeReport/BWIBBU_ALL": VALUATION,
    "/v1/exchangeReport/MI_MARGN": MARGIN,
}


@pytest.fixture
def stock_api():
    app = create_app(Settings(_env_file=None, app_env="testing", postgres_password="test-only"))
    calls = []

    async def forbidden_db():
        raise AssertionError("Stock queries must not open a database session")
        yield

    app.dependency_overrides[get_session] = forbidden_db

    def upstream(request):
        calls.append(request)
        assert not request.url.query
        assert request.headers["cache-control"] == "no-cache"
        return httpx.Response(200, json=SOURCE_PATHS[request.url.path])

    def use_source(handler):
        async def service():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                yield TwseService(client)

        app.dependency_overrides[get_twse_service] = service

    use_source(upstream)
    with TestClient(app) as client:
        yield client, calls, use_source


@pytest.mark.parametrize(
    "path,source",
    [
        ("/api/v1/stocks?q=0050", "/v1/exchangeReport/STOCK_DAY_ALL"),
        ("/api/v1/stocks/0050", "/v1/exchangeReport/STOCK_DAY_ALL"),
        ("/api/v1/stocks/2330/valuation", "/v1/exchangeReport/BWIBBU_ALL"),
        ("/api/v1/stocks/2330/margin", "/v1/exchangeReport/MI_MARGN"),
    ],
)
def test_stock_routes_fetch_their_source_on_every_call_without_database(stock_api, path, source):
    client, calls, _ = stock_api
    first, second = client.get(path), client.get(path)
    assert first.status_code == second.status_code == 200
    assert [request.url.path for request in calls] == [source, source]
    assert second.headers["cache-control"] == "no-store"
    assert second.json()["meta"]["retrieval"] == "on_request"
    assert second.json()["meta"]["source_url"].endswith(source)


def test_stock_search_filters_names_and_codes_then_paginates(stock_api):
    client, _, _ = stock_api
    body = client.get("/api/v1/stocks?q=測試&limit=1&page=2").json()
    assert body["pagination"] == {"page": 2, "limit": 1, "total": 2}
    assert [row["code"] for row in body["data"]] == ["2330"]
    assert client.get("/api/v1/stocks?q=不存在").json()["data"] == []
    assert client.get("/api/v1/stocks?q=ETF").json()["data"][0]["code"] == "0050"
    assert client.get("/api/v1/stocks?page=100").json()["data"] == []


def test_exact_quote_strings_and_separate_source_dates(stock_api):
    client, _, _ = stock_api
    quote = client.get("/api/v1/stocks/0050").json()
    assert quote["data"]["code"] == "0050"
    assert quote["data"]["closing_price"] == "123.4500"
    assert quote["data"]["trade_volume"] == "12345678901234567890"
    assert quote["data"]["opening_price"] is None
    assert quote["meta"]["data_date"] == "1151016"
    value = client.get("/api/v1/stocks/2330/valuation").json()
    assert value["meta"]["data_date"] == "1151015"
    assert value["data"]["pe_ratio"] == "20.0000"
    assert value["data"]["dividend_yield"] == ""
    margin = client.get("/api/v1/stocks/2330/margin").json()
    assert margin["meta"]["data_date"] is None
    assert "來源未提供資料日期" in margin["meta"]["note"]
    assert margin["data"]["margin_balance"] == "0"
    assert margin["data"]["short_balance"] is None


def test_unknown_quote_is_distinct_from_unpublished_optional_sections(stock_api):
    client, _, _ = stock_api
    unknown = client.get("/api/v1/stocks/999999")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "STOCK_NOT_FOUND"
    for section in ("valuation", "margin"):
        response = client.get(f"/api/v1/stocks/0050/{section}")
        assert response.status_code == 200
        assert response.json()["data"] is None
        assert response.json()["meta"]["retrieved_at"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/stocks?q=" + "a" * 101,
        "/api/v1/stocks?page=0",
        "/api/v1/stocks?limit=51",
        "/api/v1/stocks?date=1151016",
        "/api/v1/stocks/ABC",
        "/api/v1/stocks/abcdef",
        "/api/v1/stocks/1234567",
        "/api/v1/stocks/2330?url=https://example.com",
        "/api/v1/stocks/2330/margin?date=1151016",
    ],
)
def test_invalid_stock_queries_never_reach_twse(stock_api, path):
    client, calls, _ = stock_api
    assert client.get(path).status_code == 422
    assert not calls


@pytest.mark.parametrize(
    "payload",
    [
        [{"Code": "0050"}],
        [DAILY[0], DAILY[0]],
        [{"Code": None, "Name": "測試"}],
    ],
)
def test_unusable_identities_fail_instead_of_mixing_stock_data(stock_api, payload):
    client, _, use_source = stock_api
    use_source(lambda _: httpx.Response(200, json=payload))
    response = client.get("/api/v1/stocks/0050")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SOURCE_DATA_INVALID"


def test_failure_after_success_does_not_return_previous_stock_quote(stock_api):
    client, _, use_source = stock_api
    assert client.get("/api/v1/stocks/0050").status_code == 200
    failed_calls = []

    def failed(request):
        failed_calls.append(request)
        return httpx.Response(429)

    use_source(failed)
    response = client.get("/api/v1/stocks/0050")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SOURCE_RATE_LIMITED"
    assert "data" not in response.json()
    assert len(failed_calls) == 1
