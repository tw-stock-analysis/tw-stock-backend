"""HTTP boundary derives owner from session and enforces CSRF for every mutation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_authentication_api import WRITE_HEADERS, authentication_client, session_cookie

from app.api.v1.endpoints.watchlists import service
from app.services.watchlists import WatchlistStock, WatchlistView


@pytest.fixture
def api():
    with authentication_client() as h:
        item = WatchlistView(
            uuid4(), "研究", datetime.now(UTC), (WatchlistStock("TWSE", "2330", 0),)
        )
        methods = SimpleNamespace(
            **{
                name: AsyncMock()
                for name in (
                    "create",
                    "list",
                    "get",
                    "rename",
                    "delete",
                    "add_item",
                    "remove_item",
                    "reorder",
                )
            }
        )
        methods.create.return_value = item.id
        methods.get.return_value = item
        methods.list.return_value = [item]
        h.app.dependency_overrides[service] = lambda: methods
        session_cookie(h)
        yield h, methods, item


def test_authenticated_roundtrip_contract_uses_only_session_owner(api):
    h, methods, item = api
    base = f"/api/v1/watchlists/{item.id}"
    assert h.client.get("/api/v1/watchlists").json()["data"][0]["items"][0]["stock_code"] == "2330"
    for method, path, body, expected in [
        ("POST", "/api/v1/watchlists", {"name": "研究"}, 201),
        ("GET", base, None, 200),
        ("PUT", base, {"name": "新名稱"}, 200),
        ("POST", base + "/items", {"code": "2330"}, 200),
        ("PUT", base + "/order", {"codes": ["2330"]}, 200),
        ("DELETE", base + "/items/2330", None, 200),
        ("DELETE", base, None, 204),
    ]:
        response = h.client.request(method, path, json=body, headers=WRITE_HEADERS)
        assert response.status_code == expected, response.text
        assert response.headers["Cache-Control"] == "no-store"
    for name in vars(methods):
        for call in getattr(methods, name).call_args_list:
            assert call.args[0] == h.current.user_id


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("POST", "", {"name": "研究"}),
        ("PUT", "/{id}", {"name": "修改"}),
        ("DELETE", "/{id}", None),
        ("POST", "/{id}/items", {"code": "2330"}),
        ("DELETE", "/{id}/items/2330", None),
        ("PUT", "/{id}/order", {"codes": []}),
    ],
)
def test_all_writes_require_csrf_and_never_reach_service(api, method, suffix, body):
    h, methods, item = api
    response = h.client.request(method, "/api/v1/watchlists" + suffix.format(id=item.id), json=body)
    assert response.status_code == 403
    assert all(not getattr(methods, name).called for name in vars(methods))


def test_anonymous_invalid_extra_fields_and_body_bound(api):
    h, methods, _ = api
    h.client.cookies.clear()
    assert h.client.get("/api/v1/watchlists").status_code == 401
    session_cookie(h)
    assert (
        h.client.post(
            "/api/v1/watchlists",
            headers=WRITE_HEADERS,
            json={"name": "研究", "user_id": str(uuid4())},
        ).status_code
        == 422
    )
    assert (
        h.client.post(
            "/api/v1/watchlists", headers=WRITE_HEADERS, content='{"name":"' + "a" * 17000 + '"}'
        ).status_code
        == 413
    )
    assert not methods.create.called
