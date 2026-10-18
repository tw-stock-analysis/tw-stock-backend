from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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
