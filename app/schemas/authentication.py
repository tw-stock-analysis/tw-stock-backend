from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from app.schemas.common import APIModel


class LoginRequest(APIModel):
    email: str = Field(min_length=1, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or len(normalized) > 254:
            raise ValueError("Email is required and must fit within 254 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("Email must not contain control characters")
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("Email must be valid UTF-8") from error
        return normalized

    @field_validator("password")
    @classmethod
    def limit_password_bytes(cls, value: SecretStr) -> SecretStr:
        try:
            size = len(value.get_secret_value().encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("Password must be valid UTF-8") from error
        if size > 1024:
            raise ValueError("Password must fit within 1024 UTF-8 bytes")
        return value


class UserResponse(APIModel):
    id: UUID
    email: str
    display_name: str | None
    status: Literal["active"]
    email_verified_at: datetime


class LoginResponse(APIModel):
    user: UserResponse
    expires_at: datetime


class SessionResponse(APIModel):
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    created_ip: str | None
    is_current: bool

    @field_validator("created_ip", mode="before")
    @classmethod
    def stringify_ip(cls, value):
        return str(value) if value is not None else None


class SessionsResponse(APIModel):
    sessions: list[SessionResponse]
