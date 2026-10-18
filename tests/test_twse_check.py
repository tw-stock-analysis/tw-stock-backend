"""Synthetic tests for the live checker's failure detection; no source requests."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import twse_check
from app.services.twse_catalog import BY_ID


@pytest.mark.parametrize("array_schema", [False, True])
async def test_checker_compares_real_columns_to_documented_columns(
    monkeypatch, capsys, array_schema
):
    datasets = [BY_ID["daily-prices"], BY_ID["average-prices"]]
    schema = {"properties": {"Code": {"type": "string"}}}
    if array_schema:
        schema = {"items": schema}
    paths = {ds.path: {"get": {"responses": {"200": {"schema": schema}}}} for ds in datasets}
    calls = []

    def source(request):
        calls.append(request.url.path)
        assert request.headers["cache-control"] == "no-cache"
        if request.url.path.endswith("swagger.json"):
            return httpx.Response(200, json={"paths": paths})
        if request.url.path.endswith(datasets[0].path):
            return httpx.Response(200, json=[{"Code": "0050"}])
        return httpx.Response(200, json=[{"Changed": "0050"}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(source))
    monkeypatch.setattr(twse_check.httpx, "AsyncClient", lambda **_: client)
    pause = AsyncMock()
    monkeypatch.setattr(twse_check.asyncio, "sleep", pause)
    assert await twse_check.check([ds.id for ds in datasets]) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["status"] == "ok"
    assert records[0]["column_contract"] == "matched"
    assert "retrieved_at" in records[0]
    assert records[1]["code"] == "SOURCE_CONTRACT_CHANGED"
    assert records[1]["column_contract"] == "mismatch"
    assert records[1]["undocumented_columns"] == ["Changed"]
    assert records[1]["absent_columns"] == ["Code"]
    assert records[2]["passed"] == records[2]["failed"] == 1
    assert calls == ["/v1/swagger.json", *("/v1" + ds.path for ds in datasets)]
    pause.assert_awaited_once_with(1)


async def test_blocked_preflight_does_not_request_datasets(monkeypatch, capsys):
    calls = []

    def blocked(request):
        calls.append(request.url.path)
        raise httpx.ProxyError("private proxy details")

    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    monkeypatch.setattr(twse_check.httpx, "AsyncClient", lambda **_: client)
    assert await twse_check.check([]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["verified_datasets"] == 0
    assert calls == ["/v1/swagger.json"]
    assert "private proxy" not in output


async def test_valid_empty_response_does_not_claim_column_verification(monkeypatch, capsys):
    dataset = BY_ID["daily-prices"]

    def empty(request):
        if request.url.path.endswith("swagger.json"):
            return httpx.Response(
                200,
                json={
                    "paths": {
                        dataset.path: {
                            "get": {"responses": {"200": {"schema": {"properties": {"Code": {}}}}}}
                        }
                    }
                },
            )
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
    monkeypatch.setattr(twse_check.httpx, "AsyncClient", lambda **_: client)
    assert await twse_check.check([dataset.id]) == 0
    record = json.loads(capsys.readouterr().out.splitlines()[0])
    assert record["row_count"] == 0
    assert record["column_contract"] == "empty"
