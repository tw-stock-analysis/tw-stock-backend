"""Account writes with caller-prepared password hashes; no HTTP authentication endpoints."""

from uuid import UUID

from app.models import User
from app.services.account_state import (
    SessionFactory,
    database_now,
    require_user,
    revoke_credentials,
    transaction,
)
from app.services.credentials import consume_verification
from app.services.errors import ServiceError


def require_password_hash(value: str) -> None:
    # Password hashing/validation belongs to the future authentication boundary.
    # These internal methods accept an already computed Argon2id encoded hash only.
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

    async def verify_email(self, token: str) -> None:
        async with transaction(self.factory) as session:
            user, now = await consume_verification(session, token, "verify_email")
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
