"""Trusted account writes with hashes prepared outside the database transaction."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.models import User
from app.services.account_state import (
    SessionFactory,
    database_now,
    require_user,
    revoke_credentials,
    transaction,
)
from app.services.credentials import (
    IssuedCredential,
    consume_verification,
    invalid_token,
    issue_verification,
)
from app.services.errors import ServiceError


def require_password_hash(value: str) -> None:
    # Trusted callers compute hashes using PasswordService before entering a DB
    # transaction. This prefix check is not raw-password validation or hashing.
    if not value.startswith("$argon2id$"):
        raise ValueError("An already computed Argon2id password hash is required")


class AccountService:
    def __init__(self, factory: SessionFactory):
        self.factory = factory

    async def create(self, email: str, *, password_hash: str) -> UUID:
        require_password_hash(password_hash)
        normalized = email.strip().lower()
        if not normalized or len(normalized) > 254:
            raise ServiceError("EMAIL_INVALID", "Email 格式不正確。", 422)
        async with transaction(self.factory) as session:
            record = User(email=normalized, password_hash=password_hash)
            session.add(record)
            await session.flush()
            result = record.id
        return result

    async def soft_delete(self, user_id: UUID) -> None:
        async with transaction(self.factory) as session:
            user = await require_user(
                session,
                user_id,
                lock=True,
                allow_deleted=True,
                statuses=("active", "pending_verification", "disabled"),
            )
            now = await database_now(session)
            if user.deleted_at is None:
                user.deleted_at = now
                user.status = "disabled"
                user.updated_at = now
            await revoke_credentials(session, user_id, now)

    async def registration_code(self, email: str) -> IssuedCredential | None:
        async with transaction(self.factory) as session:
            await session.execute(
                insert(User)
                .values(email=email, password_hash="!unusable-until-verified")
                .on_conflict_do_nothing(
                    index_elements=[func.lower(User.email)], index_where=User.deleted_at.is_(None)
                )
            )
            user = await session.scalar(
                select(User).where(User.email == email, User.deleted_at.is_(None)).with_for_update()
            )
            if user is None or user.status != "pending_verification" or user.email_verified_at:
                return None
            return await issue_verification(session, user.id, "verify_email", timedelta(minutes=30))

    async def verify_email(
        self, token: str, *, password_hash: str | None = None, display_name: str | None = None
    ) -> None:
        if password_hash is not None:
            require_password_hash(password_hash)
        async with transaction(self.factory) as session:
            user, now = await consume_verification(session, token, "verify_email")
            if password_hash is not None:
                if user.status != "pending_verification" or user.email_verified_at is not None:
                    raise invalid_token()
                user.password_hash = password_hash
                user.display_name = display_name
            user.email_verified_at = now
            user.status = "active"
            user.updated_at = now

    async def reset_password(self, token: str, *, password_hash: str) -> None:
        require_password_hash(password_hash)
        async with transaction(self.factory) as session:
            user, now = await consume_verification(session, token, "reset_password")
            user.password_hash = password_hash
            user.updated_at = now
            await revoke_credentials(session, user.id, now)
