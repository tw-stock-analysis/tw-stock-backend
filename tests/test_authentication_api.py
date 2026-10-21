"""Browser HTTP authentication contracts; database lifecycle tests live separately."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from ipaddress import ip_address
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth_deps import get_authentication_service
from app.core.config import Settings
from app.core.login_limits import LoginRateLimiter
from app.main import create_app
from app.services.authentication import CurrentSession, LoginResult, SessionView, UserView
from app.services.credentials import IssuedCredential
from app.services.errors import ServiceError

ORIGIN = "https://app.example.test"
TOKEN = "a" * 43
PASSWORD = "private password 大小寫"
MUTATION_PATHS = (
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/me/sessions/revoke-others",
)
WRITE_HEADERS = {
    "Origin": ORIGIN,
    "X-CSRF-Protection": "1",
    "Content-Type": "application/json",
}


def mock_authentication():
    now = datetime.now(UTC)
    user = UserView(uuid4(), "member@example.test", "Member", "active", now)
    current = CurrentSession(uuid4(), user.id, user)
    expires_at = now + timedelta(days=7)
    service = SimpleNamespace(
        login=AsyncMock(
            return_value=LoginResult(user, IssuedCredential(current.id, expires_at, TOKEN))
        ),
        authenticate=AsyncMock(return_value=current),
        logout=AsyncMock(),
        revoke_others=AsyncMock(),
        list_sessions=AsyncMock(
            return_value=(
                SessionView(
                    current.id,
                    now,
                    now,
                    expires_at,
                    "Browser",
                    ip_address("192.0.2.42"),
                    True,
                ),
            )
        ),
    )
    return service, current


@contextmanager
def authentication_client(*, production=False, raise_server_exceptions=True, root_path=""):
    service, current = mock_authentication()
    settings = Settings(
        _env_file=None,
        app_env="production" if production else "testing",
        postgres_password="test-only",
        auth_allowed_origins=[ORIGIN],
    )
    app = create_app(settings)
    app.dependency_overrides[get_authentication_service] = lambda: service

    def forbidden_database(*_args, **_kwargs):
        raise AssertionError("HTTP contract tests must not access a database")

    with TestClient(
        app, base_url=ORIGIN, raise_server_exceptions=raise_server_exceptions, root_path=root_path
    ) as client:
        app.state.session_factory = forbidden_database
        yield SimpleNamespace(
            app=app, client=client, service=service, current=current, settings=settings
        )


@pytest.fixture
def auth():
    with authentication_client() as harness:
        yield harness


def session_cookie(harness, value=TOKEN):
    harness.client.cookies.set(harness.settings.session_cookie_name, value)


def post_mutation(harness, path, headers):
    if path == "/api/v1/auth/login":
        return harness.client.post(
            path,
            headers=headers,
            json={"email": "member@example.test", "password": PASSWORD},
        )
    return harness.client.post(path, headers=headers)


@pytest.mark.parametrize("production", [False, True])
def test_login_sets_host_only_httponly_cookie_and_never_returns_credentials(production):
    with authentication_client(production=production) as auth:
        response = auth.client.post(
            "/api/v1/auth/login",
            headers=WRITE_HEADERS,
            json={"email": "  MEMBER@EXAMPLE.TEST ", "password": PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["id"] == str(auth.current.user_id)
        assert body["user"]["email"] == "member@example.test"
        assert set(body) == {"user", "expires_at"}
        assert TOKEN not in response.text and PASSWORD not in response.text
        assert "password_hash" not in response.text and "token_hash" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"]
        cookie = SimpleCookie(response.headers["set-cookie"])[auth.settings.session_cookie_name]
        assert cookie.value == TOKEN
        assert cookie["httponly"] and cookie["path"] == "/"
        assert cookie["samesite"].lower() == "lax" and not cookie["domain"]
        assert int(cookie["max-age"]) == 7 * 24 * 60 * 60
        assert bool(cookie["secure"]) is production
        assert auth.settings.session_cookie_name == ("__Host-session" if production else "session")
        args, kwargs = auth.service.login.await_args
        assert (args[0] if args else kwargs["email"]) == "member@example.test"
        assert (args[1] if len(args) > 1 else kwargs["password"]) == PASSWORD


CSRF_REJECTIONS = (
    ("missing-origin", {"Origin": None}),
    ("null-origin", {"Origin": "null"}),
    ("foreign-origin", {"Origin": "https://evil.example"}),
    ("sibling-origin", {"Origin": "https://other.example.test"}),
    ("suffix-origin", {"Origin": ORIGIN + ".evil.example"}),
    ("wrong-scheme", {"Origin": "http://app.example.test"}),
    ("origin-with-path", {"Origin": ORIGIN + "/"}),
    ("multiple-origins", {"Origin": ORIGIN + " https://evil.example"}),
    ("missing-header", {"X-CSRF-Protection": None}),
    ("wrong-header", {"X-CSRF-Protection": "0"}),
    ("form-content-type", {"Content-Type": "application/x-www-form-urlencoded"}),
    ("text-content-type", {"Content-Type": "text/plain"}),
)


@pytest.mark.parametrize("path", MUTATION_PATHS)
@pytest.mark.parametrize("_case,changes", CSRF_REJECTIONS, ids=[row[0] for row in CSRF_REJECTIONS])
def test_mutations_require_trusted_origin_custom_header_and_json(auth, path, _case, changes):
    session_cookie(auth)
    headers = dict(WRITE_HEADERS)
    for key, value in changes.items():
        if value is None:
            headers.pop(key)
        else:
            headers[key] = value
    response = post_mutation(auth, path, headers)
    assert response.status_code in (403, 415)
    for operation in ("login", "logout", "revoke_others"):
        getattr(auth.service, operation).assert_not_awaited()
    assert "set-cookie" not in response.headers
    assert TOKEN not in response.text and PASSWORD not in response.text


@pytest.mark.parametrize("path", MUTATION_PATHS)
def test_duplicate_origin_headers_are_rejected(auth, path):
    session_cookie(auth)
    headers = list(WRITE_HEADERS.items()) + [("Origin", "https://evil.example")]
    response = post_mutation(auth, path, headers)
    assert response.status_code == 403
    for operation in ("login", "logout", "revoke_others"):
        getattr(auth.service, operation).assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"email": "member@example.test"},
        {"email": "member@example.test", "password": ""},
        {"email": "member@example.test", "password": 123456},
        {"email": "member@example.test", "password": PASSWORD, "user_id": str(uuid4())},
        {"email": "member@example.test", "password": PASSWORD, "password_hash": "$argon2id$raw"},
        {"email": "member@example.test", "password": "密" * 342},
        {"email": "member@example.test", "password": "a" * 1025},
    ],
)
def test_login_rejects_invalid_payloads_without_echoing_input(auth, body, caplog):
    response = auth.client.post("/api/v1/auth/login", headers=WRITE_HEADERS, json=body)
    assert response.status_code == 422
    auth.service.login.assert_not_awaited()
    assert PASSWORD not in response.text and PASSWORD not in caplog.text
    assert "$argon2id$raw" not in response.text
    assert all("input" not in field for field in response.json()["error"]["field_errors"])


def test_login_failure_does_not_set_cookie_or_echo_secret(auth, caplog):
    auth.service.login.side_effect = ServiceError("LOGIN_INVALID", "無法登入。", 401)
    response = auth.client.post(
        "/api/v1/auth/login",
        headers=WRITE_HEADERS,
        json={"email": "member@example.test", "password": PASSWORD},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "LOGIN_INVALID"
    assert "set-cookie" not in response.headers
    assert PASSWORD not in response.text and PASSWORD not in caplog.text


@pytest.mark.parametrize(
    "error",
    [SQLAlchemyError("secret DB credentials " + TOKEN), RuntimeError("secret " + PASSWORD)],
)
def test_login_errors_do_not_leak_credentials(error, caplog):
    with authentication_client(raise_server_exceptions=False) as auth:
        auth.service.login.side_effect = error
        response = auth.client.post(
            "/api/v1/auth/login",
            headers=WRITE_HEADERS,
            json={"email": "member@example.test", "password": PASSWORD},
        )
    assert response.status_code in (500, 503)
    assert "set-cookie" not in response.headers
    assert TOKEN not in response.text and TOKEN not in caplog.text
    assert PASSWORD not in response.text and PASSWORD not in caplog.text


@pytest.mark.parametrize("path", ["/api/v1/me", "/api/v1/me/sessions"])
def test_private_get_requires_cookie_without_opening_database(auth, path):
    response = auth.client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_INVALID"
    auth.service.authenticate.assert_not_awaited()


def test_current_user_and_devices_are_derived_only_from_authenticated_cookie(auth):
    session_cookie(auth)
    response = auth.client.get("/api/v1/me")
    assert response.status_code == 200
    assert response.json()["id"] == str(auth.current.user_id)
    assert response.json()["email"] == auth.current.user.email
    assert TOKEN not in response.text
    auth.service.authenticate.assert_awaited_once_with(TOKEN)
    response = auth.client.get("/api/v1/me/sessions")
    assert response.status_code == 200
    data = response.json()["sessions"]
    assert len(data) == 1 and data[0]["is_current"] is True
    assert data[0]["id"] == str(auth.current.id)
    assert data[0]["created_ip"] == "192.0.2.42"
    assert "token" not in response.text
    auth.service.list_sessions.assert_awaited_once_with(auth.current)


@pytest.mark.parametrize("path", ["/api/v1/me", "/api/v1/me/sessions"])
def test_invalid_session_is_401_and_does_not_return_private_data(auth, path):
    session_cookie(auth)
    auth.service.authenticate.side_effect = ServiceError("SESSION_INVALID", "憑證無效。", 401)
    response = auth.client.get(path)
    assert response.status_code == 401
    assert auth.current.user.email not in response.text and TOKEN not in response.text
    auth.service.list_sessions.assert_not_awaited()


@pytest.mark.parametrize("production", [False, True])
def test_logout_is_empty_204_revokes_cookie_and_deletes_matching_cookie(production):
    with authentication_client(production=production) as auth:
        session_cookie(auth)
        response = auth.client.post("/api/v1/auth/logout", headers=WRITE_HEADERS)
        assert response.status_code == 204 and response.content == b""
        auth.service.logout.assert_awaited_once_with(TOKEN)
        cookie = SimpleCookie(response.headers["set-cookie"])[auth.settings.session_cookie_name]
        assert int(cookie["max-age"]) == 0 and cookie["path"] == "/"
        assert bool(cookie["secure"]) is production
        assert cookie["httponly"] and cookie["samesite"].lower() == "lax"
        assert not cookie["domain"]


def test_logout_without_cookie_is_idempotent(auth):
    response = auth.client.post("/api/v1/auth/logout", headers=WRITE_HEADERS)
    assert response.status_code == 204 and response.content == b""
    auth.service.logout.assert_awaited_once_with(None)
    auth.service.authenticate.assert_not_awaited()


def test_revoke_others_uses_current_identity_and_returns_empty_204(auth):
    session_cookie(auth)
    response = auth.client.post("/api/v1/me/sessions/revoke-others", headers=WRITE_HEADERS)
    assert response.status_code == 204 and response.content == b""
    auth.service.revoke_others.assert_awaited_once_with(auth.current)
    assert "set-cookie" not in response.headers


def test_stale_session_cannot_revoke_others_after_dependency_validation(auth):
    session_cookie(auth)
    auth.service.revoke_others.side_effect = ServiceError("SESSION_INVALID", "憑證無效。", 401)
    response = auth.client.post("/api/v1/me/sessions/revoke-others", headers=WRITE_HEADERS)
    assert response.status_code == 401
    assert TOKEN not in response.text


@pytest.mark.parametrize("path", ["/api/v1/health/live", "/api/v1/twse/datasets"])
def test_public_routes_ignore_invalid_cookie_without_auth_or_database_calls(auth, path):
    session_cookie(auth, "invalid-cookie")
    response = auth.client.get(path)
    assert response.status_code == 200
    auth.service.authenticate.assert_not_awaited()


def test_only_private_endpoints_require_cookie_in_openapi(auth):
    schema = auth.app.openapi()
    schemes = schema["components"]["securitySchemes"]
    cookie_schemes = {
        name
        for name, value in schemes.items()
        if value.get("type") == "apiKey" and value.get("in") == "cookie"
    }
    assert cookie_schemes
    assert any(
        schemes[name]["name"] == auth.settings.session_cookie_name for name in cookie_schemes
    )
    for path, method in (
        ("/api/v1/me", "get"),
        ("/api/v1/me/sessions", "get"),
        ("/api/v1/me/sessions/revoke-others", "post"),
    ):
        security = schema["paths"][path][method]["security"]
        assert any(cookie_schemes.intersection(requirement) for requirement in security)
    assert not schema["paths"]["/api/v1/auth/login"]["post"].get("security")
    assert not schema["paths"]["/api/v1/twse/datasets"]["get"].get("security")


def test_untrusted_cors_preflight_cannot_enable_cookie_mutations(auth):
    response = auth.client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-protection",
        },
    )
    assert response.headers.get("access-control-allow-origin") not in ("*", "https://evil.example")
    auth.service.login.assert_not_awaited()


@pytest.mark.parametrize("path", MUTATION_PATHS)
def test_deployment_path_prefix_cannot_bypass_csrf(path):
    with authentication_client(root_path="/gateway") as auth:
        response = auth.client.post(f"/gateway{path}")
        assert response.status_code == 403
        auth.service.login.assert_not_awaited()
        auth.service.logout.assert_not_awaited()
        auth.service.authenticate.assert_not_awaited()
        auth.service.revoke_others.assert_not_awaited()


@pytest.mark.parametrize("path", MUTATION_PATHS[1:])
@pytest.mark.parametrize("body", [{}, {"user_id": "someone-else"}, {"current_password": PASSWORD}])
def test_bodyless_operations_reject_body_before_authentication(auth, path, body):
    session_cookie(auth)
    response = auth.client.post(path, headers=WRITE_HEADERS, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BODY_NOT_ALLOWED"
    auth.service.authenticate.assert_not_awaited()
    auth.service.logout.assert_not_awaited()
    auth.service.revoke_others.assert_not_awaited()


@pytest.mark.parametrize(
    "email", ["bad\x00@example.test", "bad\x1f@example.test", "\ud800@example.test"]
)
def test_invalid_email_unicode_and_controls_are_rejected_before_database(auth, email):
    response = auth.client.post(
        "/api/v1/auth/login",
        headers=WRITE_HEADERS,
        content=json.dumps({"email": email, "password": PASSWORD}),
    )
    assert response.status_code == 422
    auth.service.login.assert_not_awaited()
    assert PASSWORD not in response.text


@pytest.mark.parametrize("chunked", [False, True])
def test_login_body_limit_checks_actual_bytes_before_parsing(auth, chunked):
    chunks = [b"x" * 9000, b"x" * 9000]
    response = auth.client.post(
        "/api/v1/auth/login",
        headers=WRITE_HEADERS,
        content=iter(chunks) if chunked else b"".join(chunks),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "BODY_TOO_LARGE"
    auth.service.login.assert_not_awaited()


def test_email_attempts_are_limited_before_password_verification(auth):
    auth.app.state.login_limiter = LoginRateLimiter(email_limit=1)
    body = {"email": "member@example.test", "password": PASSWORD}
    assert (
        auth.client.post("/api/v1/auth/login", headers=WRITE_HEADERS, json=body).status_code == 200
    )
    body["email"] = " MEMBER@EXAMPLE.TEST "
    blocked = auth.client.post("/api/v1/auth/login", headers=WRITE_HEADERS, json=body)
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "60"
    assert blocked.json()["error"]["code"] == "LOGIN_RATE_LIMITED"
    assert auth.service.login.await_count == 1


def test_ip_limit_cannot_be_bypassed_with_untrusted_forwarded_header(auth):
    auth.app.state.login_limiter = LoginRateLimiter(ip_limit=1)
    body = {"email": "member@example.test", "password": PASSWORD}
    assert (
        auth.client.post("/api/v1/auth/login", headers=WRITE_HEADERS, json=body).status_code == 200
    )
    blocked = auth.client.post(
        "/api/v1/auth/login",
        headers={**WRITE_HEADERS, "X-Forwarded-For": "203.0.113.17"},
        json={**body, "email": "other@example.test"},
    )
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "60"
    assert auth.service.login.await_count == 1


def test_limit_capacity_exhaustion_fails_closed_without_password_work(auth):
    # The admitted IP fills the counter store before the email bucket is admitted.
    auth.app.state.login_limiter = LoginRateLimiter(max_keys=1)
    response = auth.client.post(
        "/api/v1/auth/login",
        headers=WRITE_HEADERS,
        json={"email": "member@example.test", "password": PASSWORD},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTH_CAPACITY_EXCEEDED"
    auth.service.login.assert_not_awaited()


@pytest.mark.parametrize(
    "origins",
    [
        [],
        ["http://app.example.test"],
        ["https://*.example.test"],
        ["https://app.example.test/"],
        ["https://app.example.test/path"],
        ["https://user:password@app.example.test"],
        ["null"],
        ["https://app.example.test:invalid"],
        ["https://app.example.test?query"],
        ["https://app.example.test#fragment"],
    ],
)
def test_production_rejects_unsafe_or_ambiguous_origin_settings(origins):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", auth_allowed_origins=origins)


def test_production_openapi_uses_production_cookie_and_required_csrf_header():
    with authentication_client(production=True) as auth:
        schema = auth.app.openapi()
        assert schema["components"]["securitySchemes"]["SessionCookie"]["name"] == "__Host-session"
        for path in MUTATION_PATHS:
            headers = schema["paths"][path]["post"]["parameters"]
            assert any(p["name"] == "X-CSRF-Protection" and p["required"] for p in headers)
