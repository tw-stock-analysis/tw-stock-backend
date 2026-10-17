from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app


def make_test_app():
    return create_app(Settings(_env_file=None, app_env="testing", postgres_password="test-only"))


def test_liveness_does_not_need_a_database():
    app = make_test_app()
    with TestClient(app) as client:
        response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]


def test_readiness_does_not_leak_database_errors():
    class BrokenSession:
        async def scalar(self, statement):
            raise SQLAlchemyError("secret database details")

    async def broken_session():
        yield BrokenSession()

    app = make_test_app()
    app.dependency_overrides[get_session] = broken_session
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "secret" not in response.text


def test_readiness_requires_migrations():
    class ReadySession:
        async def scalar(self, statement):
            return "0001_create_schemas"

    async def ready_session():
        yield ReadySession()

    app = make_test_app()
    app.dependency_overrides[get_session] = ready_session
    with TestClient(app) as client:
        assert client.get("/api/v1/health/ready").status_code == 200
