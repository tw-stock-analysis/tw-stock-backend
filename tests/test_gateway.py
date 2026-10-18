"""Run against the real Compose stack with GATEWAY_BASE_URL=http://127.0.0.1:8080."""

import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import pytest

BASE_URL = os.getenv("GATEWAY_BASE_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="Set GATEWAY_BASE_URL for Nginx tests")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(path, headers=None):
    # Local gateway traffic must not go through an inherited external HTTP proxy.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(Request(BASE_URL + path, headers=headers or {}), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


@pytest.mark.parametrize("path", ["/", "/stocks", "/stocks/demo-tsmc", "/watchlists/demo"])
def test_spa_routes_serve_the_frontend(path):
    status, headers, body = get(path)
    assert status == 200
    assert headers.get_content_type() == "text/html"
    assert b'<div id="root"></div>' in body
    assert headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("endpoint", ["live", "ready"])
def test_api_prefix_reaches_fastapi(endpoint):
    status, headers, body = get(f"/api/v1/health/{endpoint}")
    assert status == 200
    assert headers.get_content_type() == "application/json"
    assert json.loads(body)["data"]["status"] == ("ok" if endpoint == "live" else "ready")
    assert headers["X-Request-ID"]
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/api", "/api/unknown", "/api/v1/unknown"])
def test_unknown_api_routes_remain_json_404(path):
    status, headers, body = get(path)
    assert status == 404
    assert headers.get_content_type() == "application/json"
    assert json.loads(body)["detail"] == "Not Found"


def test_hashed_assets_cache_and_missing_assets_do_not_serve_spa():
    _, _, index = get("/index.html")
    assets = re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', index)
    assert assets
    for asset in assets:
        status, headers, _ = get(asset.decode())
        assert status == 200
        assert "immutable" in headers["Cache-Control"]
        assert headers.get_content_type() != "text/html"
    status, headers, body = get("/assets/nonexistent.js")
    assert status == 404
    assert "immutable" not in headers.get("Cache-Control", "")
    assert b'<div id="root"></div>' not in body


def test_redirect_preserves_public_origin_and_query_and_replaces_forwarded_headers():
    status, headers, _ = get(
        "/api/v1/health/live/?probe=1",
        {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.123"},
    )
    assert status == 307
    location = urlsplit(headers["Location"])
    origin = urlsplit(BASE_URL)
    assert (location.scheme, location.netloc) == (origin.scheme, origin.netloc)
    assert location.path == "/api/v1/health/live"
    assert location.query == "probe=1"
