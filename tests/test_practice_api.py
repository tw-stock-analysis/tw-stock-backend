from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_authentication_api import WRITE_HEADERS, authentication_client, session_cookie
from test_company_study import quote

from app.api.deps import get_request_source, get_stock_service
from app.api.v1.endpoints.practice import service
from app.services.errors import ServiceError


@pytest.fixture
def api():
    with authentication_client() as h:
        snapshot = {
            "id": uuid4(),
            "name": "練習",
            "initial_cash": "1000",
            "cash": "1000",
            "realized": "0",
            "fees_and_taxes": "0",
            "version": 1,
            "orders": [],
            "positions": [],
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        private = SimpleNamespace(
            create=AsyncMock(return_value=snapshot),
            list=AsyncMock(return_value=[snapshot]),
            order=AsyncMock(return_value=snapshot),
            reflect=AsyncMock(return_value=snapshot),
            delete=AsyncMock(),
            progress=AsyncMock(
                return_value={
                    "observe": False,
                    "note": False,
                    "paper_buy": False,
                    "reflection": False,
                }
            ),
        )
        source = SimpleNamespace(
            fetch=AsyncMock(
                return_value=(
                    [
                        {
                            "date": "2026-07-01",
                            "stock_id": "2330",
                            "open": "99",
                            "max": "101",
                            "min": "98",
                            "close": "100",
                        }
                    ],
                    None,
                )
            )
        )
        stocks = SimpleNamespace(quote=AsyncMock(return_value=quote()))
        h.app.dependency_overrides[service] = lambda: private
        h.app.dependency_overrides[get_request_source] = lambda: source
        h.app.dependency_overrides[get_stock_service] = lambda: stocks
        session_cookie(h)
        yield h, private, source, stocks, snapshot


def test_reference_is_private_exact_day_and_no_price_fallback(api):
    h, _, source, stocks, _ = api
    response = h.client.get("/api/v1/practice/reference?code=2330&day=2026-07-01")
    assert response.status_code == 200 and response.json()["close"] == "100"
    assert response.headers["Cache-Control"] == "no-store"
    source.fetch.return_value = ([], None)
    assert (
        h.client.get("/api/v1/practice/reference?code=2330&day=2026-07-01").json()["error"]["code"]
        == "PRACTICE_PRICE_MISSING"
    )
    source.fetch.side_effect = ServiceError("FINMIND_ACCESS_DENIED", "權限不足", 503)
    assert h.client.get("/api/v1/practice/reference?code=2330&day=2026-07-01").status_code == 503
    h.client.cookies.clear()
    before = stocks.quote.await_count
    assert h.client.get("/api/v1/practice/reference?code=2330&day=2026-07-01").status_code == 401
    assert stocks.quote.await_count == before


def test_private_account_contract_and_owner_derivation(api):
    h, private, source, _, snapshot = api
    assert h.client.get("/api/v1/practice/accounts").status_code == 200
    assert h.client.get("/api/v1/practice/progress").json()["paper_buy"] is False
    response = h.client.post(
        "/api/v1/practice/accounts",
        json={"name": "練習", "initial_cash": "1000"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 201 and response.json()["cash"] == "1000"
    assert private.create.call_args.args[0] == h.current.user_id
    assert not source.fetch.called
    assert (
        h.client.post(
            "/api/v1/practice/accounts",
            json={"name": "練習", "user_id": str(uuid4())},
            headers=WRITE_HEADERS,
        ).status_code
        == 422
    )
    assert (
        h.client.delete(
            f"/api/v1/practice/accounts/{snapshot['id']}?version=1", headers=WRITE_HEADERS
        ).status_code
        == 204
    )
    assert private.delete.call_args.args[0] == h.current.user_id


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/accounts", {"name": "練習"}),
        ("POST", "/accounts/{id}/orders", {}),
        ("PUT", "/accounts/{id}/orders/{id}/reflection", {}),
        ("DELETE", "/accounts/{id}?version=1", None),
    ],
)
def test_all_simulation_writes_enforce_csrf_before_parsing(api, method, path, body):
    h, private, _, _, snapshot = api
    response = h.client.request(
        method, "/api/v1/practice" + path.format(id=snapshot["id"]), json=body
    )
    assert response.status_code == 403
    assert (
        not private.create.called
        and not private.order.called
        and not private.reflect.called
        and not private.delete.called
    )


def test_practice_body_bound(api):
    h, _, _, _, _ = api
    assert (
        h.client.post(
            "/api/v1/practice/accounts",
            headers=WRITE_HEADERS,
            content='{"name":"' + "a" * 17000 + '"}',
        ).status_code
        == 413
    )
