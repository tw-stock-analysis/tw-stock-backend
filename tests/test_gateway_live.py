"""Opt-in, sequential real TWSE checks through Nginx. Never saves source records."""

import json
import os
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from app.services.twse_catalog import DATASETS

BASE = os.getenv("GATEWAY_BASE_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not BASE or os.getenv("RUN_LIVE_TWSE_TESTS") != "1",
    reason="Set GATEWAY_BASE_URL and RUN_LIVE_TWSE_TESTS=1 for real source verification",
)


def get(path):
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(Request(BASE + path), timeout=40)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, json.loads(response.read())


@pytest.mark.parametrize("dataset", DATASETS, ids=lambda ds: ds.id)
def test_real_twse_dataset_through_nginx(dataset):
    time.sleep(1)
    status, headers, body = get(dataset.endpoint)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Request-ID"]
    assert body["dataset"]["id"] == dataset.id
    assert body["meta"]["row_count"] == len(body["data"])
    assert body["meta"]["source_url"] == dataset.source_url
    assert all(isinstance(row, dict) for row in body["data"])


def test_real_stock_search_quote_and_optional_sections_through_nginx():
    status, _, search = get("/api/v1/stocks?q=2330&limit=1")
    assert status == 200
    assert search["data"][0]["code"] == "2330"
    timestamps = []
    for path in (
        "/api/v1/stocks/2330",
        "/api/v1/stocks/2330",
        "/api/v1/stocks/2330/valuation",
        "/api/v1/stocks/2330/margin",
    ):
        time.sleep(1)
        status, headers, body = get(path)
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert body["data"] is None or body["data"]["code"] == "2330"
        if path.endswith("/margin"):
            assert body["meta"]["data_date"] is None
        if path.endswith("/2330"):
            timestamps.append(body["meta"]["retrieved_at"])
    assert timestamps[0] != timestamps[1]
