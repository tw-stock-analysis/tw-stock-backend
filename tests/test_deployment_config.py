"""Production preflight rejects unsafe targets before any container changes."""

import pytest

from scripts.deploy import validate_config


def deployment_config():
    backend = "ghcr.io/tw-stock-analysis/tw-stock-backend@sha256:" + "a" * 64
    frontend = "ghcr.io/tw-stock-analysis/tw-stock-frontend@sha256:" + "b" * 64
    config = {
        "name": "tw-stock-prod",
        "services": {
            "backend": {
                "image": backend,
                "environment": {
                    "APP_ENV": "production",
                    "DATABASE_URL": "",
                    "POSTGRES_HOST": "db",
                    "AUTH_ALLOWED_ORIGINS": '["https://stocks.example.test"]',
                },
            },
            "migrate": {"image": backend},
            "web": {
                "image": frontend,
                "environment": {"SITE_HOST": "stocks.example.test"},
                "ports": [{"target": 443, "published": "443"}],
            },
            "db": {"environment": {"POSTGRES_PASSWORD": "test-only-password"}},
        },
        "volumes": {"production_postgres_data": {"name": "tw-stock-prod_production_postgres_data"}},
    }
    credentials = {
        "POSTGRES_USER": "ci_user",
        "POSTGRES_DB": "ci_deploy",
        "POSTGRES_PASSWORD": "test-only-password",
    }
    config["services"]["backend"]["environment"].update(credentials)
    config["services"]["migrate"]["environment"] = credentials.copy()
    config["services"]["db"]["environment"] = credentials.copy()
    return config


def test_accepts_pinned_images_and_isolated_production_database():
    validate_config(deployment_config())


@pytest.mark.parametrize("service", ["backend", "migrate", "web"])
def test_rejects_mutable_or_wrong_repository_images(service):
    config = deployment_config()
    config["services"][service]["image"] = "ghcr.io/tw-stock-analysis/tw-stock-backend:master"
    with pytest.raises(ValueError, match="digest"):
        validate_config(config)


@pytest.mark.parametrize("service", ["backend", "migrate", "db"])
def test_rejects_public_private_service_ports(service):
    config = deployment_config()
    config["services"][service]["ports"] = [{"published": "5432"}]
    with pytest.raises(ValueError, match="private"):
        validate_config(config)


@pytest.mark.parametrize(
    "change",
    [
        {"APP_ENV": "development"},
        {"DATABASE_URL": "postgresql+asyncpg://elsewhere/production"},
        {"POSTGRES_HOST": "shared-development-db"},
        {"AUTH_ALLOWED_ORIGINS": '["http://stocks.example.test"]'},
        {"AUTH_ALLOWED_ORIGINS": '["https://different.example.test"]'},
    ],
)
def test_rejects_wrong_environment_database_or_origin(change):
    config = deployment_config()
    config["services"]["backend"]["environment"].update(change)
    with pytest.raises(ValueError):
        validate_config(config)


def test_rejects_shared_development_project_or_volume():
    config = deployment_config()
    config["name"] = "tw-stock-dev"
    with pytest.raises(ValueError, match="separate"):
        validate_config(config)
    config["name"] = "tw-stock-prod"
    config["volumes"]["production_postgres_data"]["name"] = "tw-stock-dev_dev_postgres_data"
    with pytest.raises(ValueError, match="isolated"):
        validate_config(config)
