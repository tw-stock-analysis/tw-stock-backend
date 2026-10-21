from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "testing", "production"] = "development"
    postgres_user: str = "twstock"
    postgres_password: str = ""
    postgres_db: str = "twstock"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    database_url: str | None = None
    auth_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8080", "http://127.0.0.1:8080"]
    )
    password_max_concurrency: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def validate_auth_origins(self) -> "Settings":
        if not self.auth_allowed_origins:
            raise ValueError("AUTH_ALLOWED_ORIGINS must contain an explicit browser origin")
        for origin in self.auth_allowed_origins:
            parsed = urlsplit(origin)
            try:
                parsed.port
            except ValueError as error:
                raise ValueError("Invalid AUTH_ALLOWED_ORIGINS port") from error
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or origin != f"{parsed.scheme}://{parsed.netloc}"
                or "*" in origin
                or any(character.isspace() for character in origin)
            ):
                raise ValueError("AUTH_ALLOWED_ORIGINS must use exact origins without paths")
            if self.app_env == "production" and parsed.scheme != "https":
                raise ValueError("Production AUTH_ALLOWED_ORIGINS must use HTTPS")
        return self

    @property
    def session_cookie_name(self) -> str:
        return "__Host-session" if self.app_env == "production" else "session"

    @property
    def session_cookie_secure(self) -> bool:
        return self.app_env == "production"

    def sqlalchemy_url(self) -> URL:
        if self.database_url:
            url = make_url(self.database_url)
            if url.drivername != "postgresql+asyncpg":
                raise ValueError("DATABASE_URL must use postgresql+asyncpg")
            return url
        if not self.postgres_password or self.postgres_password == "GENERATE_WITH_SETUP":
            raise ValueError(
                "Copy backend/.env.example to backend/.env and set PostgreSQL credentials"
            )
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
