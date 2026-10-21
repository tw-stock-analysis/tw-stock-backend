"""Synthetic HTTP contracts only; these tests never stand in for live verification."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_twse_service
from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.services.errors import ServiceError
from app.services.twse import TwseService, decode_rows
from app.services.twse_catalog import BASE_URL, BY_ID, DATASETS


@pytest.fixture
def api():
    app = create_app(Settings(_env_file=None, app_env="testing", postgres_password="test-only"))

    async def forbidden_db():
        raise AssertionError("Trading queries must never open a database session")
        yield

    app.dependency_overrides[get_session] = forbidden_db
    return app


def test_catalog_is_complete_for_approved_scope_and_needs_no_source_or_database(api):
    with TestClient(api) as client:
        result = client.get("/api/v1/twse/datasets")
        prices = client.get("/api/v1/twse/datasets?category=prices").json()["data"]
        warrant = client.get("/api/v1/twse/datasets?q=t187ap42_L").json()["data"]
    assert result.status_code == 200
    rows = result.json()["data"]
    assert len(rows) == len({row["id"] for row in rows}) == 44
    assert len({row["path"] for row in rows}) == 44
    assert {row["path"].split("/")[1] for row in rows} == {
        "exchangeReport",
        "indicesReport",
        "fund",
        "block",
        "announcement",
        "Announcement",
        "holidaySchedule",
        "SBL",
        "opendata",
        "ETFReport",
    }
    assert all(row["category"] == "prices" for row in prices)
    assert [row["id"] for row in warrant] == ["warrant-trading"]
    assert not any("t187ap06" in row["path"] or "t187ap03" in row["path"] for row in rows)
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("dataset", DATASETS, ids=lambda dataset: dataset.id)
def test_every_endpoint_uses_its_own_twse_url_on_every_request(api, dataset):
    calls = []

    def upstream(request):
        calls.append(request)
        assert str(request.url) == BASE_URL + dataset.path
        assert not request.url.query
        assert request.headers["cache-control"] == "no-cache"
        return httpx.Response(200, content='[{"Code":"0050","value":12345678901234567890.123}]')

    async def mocked_service():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as source_client:
            yield TwseService(source_client)

    api.dependency_overrides[get_twse_service] = mocked_service
    with TestClient(api) as client:
        first = client.get(dataset.endpoint)
        second = client.get(dataset.endpoint)
    assert first.status_code == second.status_code == 200
    assert len(calls) == 2
    assert second.json()["dataset"]["id"] == dataset.id
    assert second.json()["data"] == [{"Code": "0050", "value": "12345678901234567890.123"}]
    assert second.json()["meta"]["retrieval"] == "on_request"
    assert second.json()["meta"]["source_url"] == dataset.source_url
    assert second.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "query", ["date=20260101", "code=2330", "url=https://example.com", "limit=1"]
)
def test_unpublished_source_parameters_are_rejected(api, query):
    with TestClient(api) as client:
        response = client.get(BY_ID["daily-prices"].endpoint + "?" + query)
    assert response.status_code == 422


def test_no_generic_proxy_or_retired_data_import_routes(api):
    with TestClient(api) as client:
        for path in (
            "/api/v1/twse/exchangeReport/T86",
            "/api/v1/twse/opendata/t187ap03_L",
            "/api/v1/realtime/2330/quote",
            "/api/v1/data-sources",
            "/api/v1/instruments",
            "/api/v1/quotes",
            "/api/v1/market/status",
        ):
            assert client.get(path).status_code == 404


def test_exact_source_values_and_optional_columns():
    content = b'\xef\xbb\xbf[{"Code":"0050","value":12345678901234567890.123,"missing":null}]'
    assert decode_rows(content) == [
        {"Code": "0050", "value": "12345678901234567890.123", "missing": None}
    ]
    assert decode_rows(b"[]") == []


@pytest.mark.parametrize(
    "content",
    [
        b"<html>maintenance</html>",
        b'{"error":"maintenance"}',
        b"[{}]",
        b'[{"value":true}]',
        b'[{"value":NaN}]',
        b'[{"x":"1","x":"2"}]',
        b'[{"value":{"nested":"error"}}]',
        b"[null]",
        b'[{"":"1"}]',
    ],
)
def test_invalid_payload_is_an_error_instead_of_empty_market_data(content):
    with pytest.raises(ServiceError, match="TWSE") as caught:
        decode_rows(content)
    assert caught.value.code == "SOURCE_DATA_INVALID"


@pytest.mark.parametrize(
    "status,code",
    [
        (429, "SOURCE_RATE_LIMITED"),
        (403, "SOURCE_UNAVAILABLE"),
        (302, "SOURCE_UNAVAILABLE"),
        (500, "SOURCE_UNAVAILABLE"),
    ],
)
async def test_upstream_failure_is_not_retried_redirected_or_cached(status, code):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example"}, text="secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = TwseService(client)
        with pytest.raises(ServiceError) as caught:
            await service.fetch(BY_ID["daily-prices"])
    assert len(calls) == 1
    assert caught.value.code == code
    assert "secret" not in str(caught.value)


async def test_a_shared_service_fetches_new_data_and_does_not_fall_back_after_failure():
    calls = []

    def upstream(request):
        calls.append(request)
        if len(calls) == 3:
            raise httpx.ProxyError("secret proxy connection details")
        return httpx.Response(200, json=[{"ClosingPrice": str(len(calls))}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = TwseService(client)
        assert (await service.fetch(BY_ID["daily-prices"])).data[0]["ClosingPrice"] == "1"
        assert (await service.fetch(BY_ID["daily-prices"])).data[0]["ClosingPrice"] == "2"
        with pytest.raises(ServiceError) as caught:
            await service.fetch(BY_ID["daily-prices"])
    assert len(calls) == 3
    assert "secret" not in str(caught.value)


async def test_new_columns_are_exposed_and_large_responses_are_bounded(monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=[{"first": "1"}, {"second": "2"}])
        )
    ) as client:
        assert (await TwseService(client).fetch(BY_ID["daily-prices"])).columns == [
            "first",
            "second",
        ]
        monkeypatch.setattr("app.services.twse.MAX_BYTES", 1)
        with pytest.raises(ServiceError) as caught:
            await TwseService(client).fetch(BY_ID["daily-prices"])
    assert caught.value.code == "SOURCE_DATA_INVALID"


def test_openapi_keeps_public_market_endpoints_explicit_typed_and_unauthenticated(api):
    schema = api.openapi()
    public_paths = {
        path: operations
        for path, operations in schema["paths"].items()
        if path.startswith(("/api/v1/twse/", "/api/v1/stocks", "/api/v1/health/"))
    }
    assert len(public_paths) == 51  # 44 TWSE + catalogue + two health + four stock views.
    assert not schema.get("security")
    for operations in public_paths.values():
        assert not operations["get"].get("security")
    for dataset in DATASETS:
        operation = schema["paths"][dataset.endpoint]["get"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("TradingResponse")
    assert "SOURCE_NOT_CONFIGURED" not in json.dumps(schema)
