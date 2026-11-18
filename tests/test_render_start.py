import subprocess
from unittest.mock import Mock

import pytest

from app import render_start
from app.core.config import Settings


@pytest.mark.parametrize("driver", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_managed_postgres_url_keeps_escaped_credentials(driver):
    settings = Settings(
        _env_file=None, database_url=f"{driver}://member:p%40ss%3Aword@db.internal:5432/twstock"
    )
    url = settings.sqlalchemy_url()
    assert url.drivername == "postgresql+asyncpg"
    assert url.password == "p@ss:word" and url.host == "db.internal"
    assert url.database == "twstock" and url.port == 5432


@pytest.mark.parametrize("driver", ["sqlite", "postgresql+psycopg", "mysql"])
def test_unrelated_drivers_remain_rejected(driver):
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        Settings(_env_file=None, database_url=f"{driver}://host/database").sqlalchemy_url()


@pytest.mark.parametrize("value", ["0", "65536", "abc", "10000;echo", "", " 8000", "８０００"])
def test_invalid_port_never_runs_a_command(value):
    with pytest.raises(ValueError):
        render_start.port(value)


def test_migration_failure_prevents_server_start(monkeypatch):
    migrate = Mock(side_effect=subprocess.CalledProcessError(1, "alembic"))
    server = Mock()
    monkeypatch.setattr(render_start.subprocess, "run", migrate)
    monkeypatch.setattr(render_start.os, "execv", server)
    monkeypatch.setenv("PORT", "10000")
    with pytest.raises(subprocess.CalledProcessError):
        render_start.main()
    assert not server.called
    assert migrate.call_args.kwargs["check"] is True


def test_success_executes_server_with_render_port_after_migration(monkeypatch):
    calls = []
    monkeypatch.setattr(render_start.subprocess, "run", lambda *a, **kw: calls.append("migration"))
    monkeypatch.setattr(render_start.os, "execv", lambda executable, argv: calls.append(argv))
    monkeypatch.setenv("PORT", "12345")
    render_start.main()
    assert calls[0] == "migration"
    assert calls[1][calls[1].index("--port") + 1] == "12345"
    assert calls[1][calls[1].index("--host") + 1] == "0.0.0.0"
