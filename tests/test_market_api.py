from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.models.market import Instrument
from app.services.market_import import publish_snapshot


def make_app():
    return create_app(Settings(_env_file=None, app_env="testing", postgres_password="test-only"))


def test_openapi_exposes_typed_public_contracts_without_auth():
    with TestClient(make_app()) as client:
        spec = client.get("/api/v1/openapi.json").json()
        assert client.get("/api/v1/health/live").json()["data"]["status"] == "ok"
    paths = spec["paths"]
    assert set(paths) == {
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/instruments",
        "/api/v1/instruments/{instrument_id}/overview",
        "/api/v1/quotes",
        "/api/v1/market/status",
    }
    assert not spec.get("components", {}).get("securitySchemes")
    for path in paths.values():
        operation = path["get"]
        assert not operation.get("security")
        assert "$ref" in operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" in operation["responses"]["422"]["content"]["application/json"]["schema"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/instruments?page=0",
        "/api/v1/instruments?limit=51",
        "/api/v1/instruments?market=INVALID",
        "/api/v1/instruments?q=" + "a" * 81,
        "/api/v1/instruments?unexpected=1",
        "/api/v1/instruments/not-a-uuid/overview",
        "/api/v1/quotes",
        "/api/v1/quotes?ids=not-a-uuid",
        "/api/v1/quotes?" + "&".join("ids=" + str(uuid4()) for _ in range(101)),
    ],
)
def test_request_models_reject_invalid_input_before_database_queries(path):
    with TestClient(make_app()) as client:
        response = client.get(path)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["field_errors"]
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_unsupported_market_is_explicit_instead_of_empty_success():
    with TestClient(make_app()) as client:
        response = client.get("/api/v1/instruments?market=TPEX")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_NOT_SUPPORTED"


def test_method_not_allowed_preserves_the_allow_header():
    with TestClient(make_app()) as client:
        response = client.post("/api/v1/instruments")
    assert response.status_code == 405
    assert "GET" in response.headers["Allow"]


def test_database_failure_is_sanitized_and_not_reported_as_empty_data():
    class BrokenSession:
        async def get(self, *args):
            raise SQLAlchemyError("private connection credentials")

    async def broken_session():
        yield BrokenSession()

    app = make_app()
    app.dependency_overrides[get_session] = broken_session
    with TestClient(app) as client:
        response = client.get("/api/v1/instruments")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "private" not in response.text


@pytest.fixture
async def api_client(db_session):
    app = make_app()

    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


@pytest.mark.integration
async def test_empty_database_reports_not_loaded_and_no_fake_prices(api_client):
    response = await api_client.get("/api/v1/market/status")
    assert response.status_code == 200
    assert all(row["status"] == "not_loaded" for row in response.json()["data"])
    for path in ["/api/v1/instruments", "/api/v1/quotes?ids=" + str(uuid4())]:
        response = await api_client.get(path)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "MARKET_DATA_UNAVAILABLE"


@pytest.mark.integration
async def test_public_queries_use_persisted_data_and_exact_decimal_strings(
    api_client,
    db_session,
    snapshot,
):
    await publish_snapshot(db_session, snapshot)
    response = await api_client.get("/api/v1/instruments", params={"q": "2330"})
    assert response.status_code == 200
    data = response.json()
    assert data["pagination"]["total"] == 1
    assert data["meta"]["data_as_of"] == "2024-01-03"
    assert data["meta"]["freshness"] == "unknown"
    instrument_id = data["data"][0]["instrumentId"]
    response = await api_client.get("/api/v1/instruments/" + instrument_id + "/overview")
    assert response.status_code == 200
    overview = response.json()["data"]
    assert overview["instrument"]["ticker"] == "2330"
    quote = overview["quote"]["quote"]
    assert quote["close"] == "593.000000"
    assert quote["volume_shares"] == "12345678"
    assert quote["meta"]["data_as_of"] == "2024-01-02"
    assert quote["meta"]["data_version"] == snapshot.prices_version
    assert "raw" not in quote

    ids = dict((await db_session.execute(select(Instrument.ticker, Instrument.id))).all())
    missing = str(uuid4())
    response = await api_client.get(
        "/api/v1/quotes",
        params=[
            ("ids", str(ids["2317"])),
            ("ids", str(ids["9999"])),
            ("ids", missing),
            ("ids", instrument_id),
            ("ids", instrument_id),
        ],
    )
    assert response.status_code == 200
    quotes = response.json()["data"]
    assert [row["status"] for row in quotes] == ["available", "no_data", "not_found", "available"]
    assert quotes[0]["quote"]["close"] is None
    assert quotes[0]["quote"]["trade_status"] == "no_trade"
    assert "close" in quotes[0]["quote"]["missing_fields"]
    assert quotes[0]["quote"]["missing_reasons"]["close"] == "NO_TRADE"
    assert quotes[1]["reason_code"] == "NO_DAILY_PRICE"
    assert quotes[2]["instrumentId"] == missing


@pytest.mark.integration
async def test_search_ranking_pagination_literal_wildcards_and_missing_id(
    api_client,
    db_session,
    snapshot,
):
    await publish_snapshot(db_session, snapshot)
    response = await api_client.get("/api/v1/instruments?q=23&limit=1")
    assert response.json()["pagination"]["total"] == 2
    assert [row["ticker"] for row in response.json()["data"]] == ["2317"]
    response = await api_client.get("/api/v1/instruments?q=23&limit=1&page=2")
    assert [row["ticker"] for row in response.json()["data"]] == ["2330"]
    response = await api_client.get("/api/v1/instruments", params={"q": "台灣積體"})
    assert response.json()["data"][0]["ticker"] == "2330"
    response = await api_client.get("/api/v1/instruments", params={"q": "%"})
    assert response.json()["data"] == []
    response = await api_client.get("/api/v1/instruments/" + str(uuid4()) + "/overview")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
