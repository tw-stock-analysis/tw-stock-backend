import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_fugle_service
from app.core.config import Settings
from app.main import create_app
from app.schemas.realtime import RealtimeMeta, RealtimeResponse
from app.services.errors import ServiceError
from app.services.fugle import BASE_URL, FugleService


def settings(**kwargs):
    return Settings(_env_file=None, postgres_password="test-only", **kwargs)


async def test_same_quote_requests_share_cache_and_credentials_stay_in_header():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["X-API-KEY"] == "test-key"
        assert "test-key" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "symbol": "2330",
                "isTrial": True,
                "lastPrice": 999,
                "lastTrial": {"price": 999},
                "lastTrade": {"price": 998},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = FugleService(client, settings(fugle_api_key="test-key"))
        results = await asyncio.gather(*(service.fetch("quote", "2330") for _ in range(5)))
    assert len(calls) == 1
    assert str(calls[0].url) == BASE_URL + "intraday/quote/2330"
    assert results[0].meta.is_trial is True
    assert results[0].data["lastTrial"]["price"] == "999"
    assert results[0].data["lastTrade"]["price"] == "998"
    assert "test-key" not in results[0].model_dump_json()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_failures_do_not_leak_upstream_details_or_use_fake_data(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="private details test-key"),
        )
    ) as client:
        service = FugleService(client, settings(fugle_api_key="test-key"))
        with pytest.raises(ServiceError) as exc:
            await service.fetch("quote", "2330")
    assert exc.value.code == "SOURCE_UNAVAILABLE"
    assert "test-key" not in exc.value.message


async def test_missing_key_and_production_display_agreement_fail_before_network():
    def handle(_):
        pytest.fail("Unconfigured source must not be contacted")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        for config, expected in [
            (settings(), "SOURCE_NOT_CONFIGURED"),
            (
                settings(app_env="production", fugle_api_key="test-key"),
                "SOURCE_DISPLAY_NOT_AUTHORIZED",
            ),
        ]:
            with pytest.raises(ServiceError) as exc:
                await FugleService(client, config).fetch("quote", "2330")
            assert exc.value.code == expected


async def test_free_tier_budget_and_429_cooldown():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = FugleService(client, settings(fugle_api_key="test-key"))
        with pytest.raises(ServiceError):
            await service.fetch("quote", "2330")
        with pytest.raises(ServiceError) as exc:
            await service.fetch("quote", "2317")
    assert exc.value.code == "SOURCE_RATE_LIMITED"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/realtime/2330/history?date_from=2025-01-01&date_to=2026-01-01",
        "/api/v1/realtime/2330/history?date_from=2026-01-02&date_to=2026-01-01",
        "/api/v1/realtime/2330/history?date_from=2026-01-01&date_to=2026-01-02&adjusted=true&timeframe=1",
        "/api/v1/realtime/2330/quote?type=unexpected",
        "/api/v1/realtime/2330/quote?api_key=secret",
    ],
)
def test_invalid_parameters_fail_before_network(path):
    with TestClient(create_app(settings(app_env="testing"))) as client:
        response = client.get(path)
    assert response.status_code == 422


def test_unconfigured_source_has_consistent_api_error():
    with TestClient(create_app(settings(app_env="testing"))) as client:
        response = client.get("/api/v1/realtime/2330/quote")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SOURCE_NOT_CONFIGURED"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


async def test_ticker_list_uses_documented_list_route_and_filters():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "date": "2026-10-16",
                "type": "EQUITY",
                "data": [{"symbol": "0050", "name": "測試"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await FugleService(client, settings(fugle_api_key="test-key")).fetch(
            "tickers",
            params={"type": "EQUITY", "market": "TSE"},
        )
    assert requests[0].url.path.endswith("/intraday/tickers")
    assert requests[0].url.params["market"] == "TSE"
    assert result.data["data"][0]["symbol"] == "0050"


def test_query_aliases_and_bounded_pagination_reach_the_service():
    from datetime import UTC, datetime

    captured = []

    class Source:
        async def fetch(self, resource, symbol="", params=None):
            captured.append((resource, symbol, params))
            return RealtimeResponse(
                data={},
                meta=RealtimeMeta(
                    source_url=BASE_URL,
                    retrieved_at=datetime.now(UTC),
                    usage="private_development",
                ),
            )

    app = create_app(settings(app_env="testing"))
    app.dependency_overrides[get_fugle_service] = lambda: Source()
    with TestClient(app) as client:
        response = client.get("/api/v1/realtime/tickers?market=TSE&isNormal=true")
        assert response.status_code == 200, response.text
        response = client.get("/api/v1/realtime/2330/trades?isTrial=true&limit=50&offset=5")
        assert response.status_code == 200, response.text
        assert client.get("/api/v1/realtime/2330/trades?limit=501").status_code == 422
    assert captured[0][2]["isNormal"] == "true"
    assert captured[1][2]["isTrial"] == "true"
    assert captured[1][2]["offset"] == 5
