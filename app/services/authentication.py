"""Browser authentication with server-side revocation and a fixed seven-day lifetime."""

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuthSession, User
from app.services.account_state import SessionFactory, database_now, transaction
from app.services.credentials import IpAddress, IssuedCredential, digest_token
from app.services.errors import ServiceError

SESSION_TTL = timedelta(days=7)
SESSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)


class PasswordVerifier(Protocol):
    async def verify(self, password: str, encoded: str | None) -> bool: ...


@dataclass(frozen=True)
class UserView:
    id: UUID
    email: str
    display_name: str | None
    status: str
    email_verified_at: datetime


@dataclass(frozen=True)
class CurrentSession:
    id: UUID
    user_id: UUID
    user: UserView


@dataclass(frozen=True)
class LoginResult:
    user: UserView
    credential: IssuedCredential


@dataclass(frozen=True)
class SessionView:
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    created_ip: IpAddress
    is_current: bool


def invalid_login() -> ServiceError:
    return ServiceError("LOGIN_INVALID", "Email 或密碼不正確，或帳號無法登入。", 401)


def invalid_session() -> ServiceError:
    return ServiceError("SESSION_INVALID", "登入憑證無效或已過期。", 401)


def is_session_token(value: str | None) -> bool:
    return isinstance(value, str) and SESSION_TOKEN_PATTERN.fullmatch(value) is not None


def _available(user: User | None) -> bool:
    return (
        user is not None
        and user.deleted_at is None
        and user.status == "active"
        and user.email_verified_at is not None
    )


def _user_view(user: User) -> UserView:
    assert user.email_verified_at is not None
    return UserView(user.id, user.email, user.display_name, user.status, user.email_verified_at)


async def _lock_user(session: AsyncSession, user_id: UUID) -> User | None:
    return await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _require_record(
    session: AsyncSession, session_id: UUID, user_id: UUID
) -> tuple[AuthSession, datetime]:
    """Caller already holds the user lock; acquire credential locks second."""
    now = await database_now(session)
    record = await session.scalar(
        select(AuthSession)
        .where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .with_for_update()
    )
    if record is None:
        raise invalid_session()
    return record, now


async def _require_current(
    session: AsyncSession, current: CurrentSession
) -> tuple[User, AuthSession, datetime]:
    # Recheck after the user lock: a dependency check may predate a logout/reset.
    user = await _lock_user(session, current.user_id)
    if not _available(user):
        raise invalid_session()
    assert user is not None
    record, now = await _require_record(session, current.id, current.user_id)
    return user, record, now


class AuthenticationService:
    def __init__(self, factory: SessionFactory, passwords: PasswordVerifier):
        self.factory = factory
        self.passwords = passwords

    async def login(
        self,
        email: str,
        password: str,
        *,
        user_agent: str | None = None,
        created_ip: IpAddress = None,
    ) -> LoginResult:
        normalized = email.strip().lower()
        async with self.factory() as session:
            candidate = (
                await session.execute(
                    select(User.id, User.password_hash).where(
                        User.email == normalized, User.deleted_at.is_(None)
                    )
                )
            ).one_or_none()
        # Release the connection before expensive hashing. Unknown accounts use
        # the password verifier's dummy hash, rather than a cheap early return.
        expected_hash = candidate.password_hash if candidate is not None else None
        verified = await self.passwords.verify(password, expected_hash)
        if not verified or candidate is None:
            raise invalid_login()
        async with transaction(self.factory) as session:
            user = await _lock_user(session, candidate.id)
            if (
                not _available(user)
                or user is None
                or user.password_hash != expected_hash
                or user.email != normalized
            ):
                raise invalid_login()
            # Both reset_password and soft_delete take this same user lock. A
            # password verified before either change cannot issue a new session.
            now = await database_now(session)
            value = secrets.token_urlsafe(32)
            record = AuthSession(
                user_id=user.id,
                token_hash=digest_token(value),
                created_at=now,
                last_seen_at=now,
                expires_at=now + SESSION_TTL,
                user_agent=user_agent[:512] if user_agent else None,
                created_ip=created_ip,
            )
            session.add(record)
            await session.flush()
            result = LoginResult(
                _user_view(user), IssuedCredential(record.id, record.expires_at, value)
            )
        # A credential is exposed only after commit succeeds.
        return result

    async def authenticate(self, value: str) -> CurrentSession:
        if not is_session_token(value):
            raise invalid_session()
        async with transaction(self.factory) as session:
            identity = (
                await session.execute(
                    select(AuthSession.id, AuthSession.user_id).where(
                        AuthSession.token_hash == digest_token(value)
                    )
                )
            ).one_or_none()
            if identity is None:
                raise invalid_session()
            user = await _lock_user(session, identity.user_id)
            if not _available(user):
                raise invalid_session()
            assert user is not None
            current = CurrentSession(identity.id, user.id, _user_view(user))
            record, now = await _require_record(session, current.id, current.user_id)
            record.last_seen_at = now
        return current

    async def list_sessions(self, current: CurrentSession) -> tuple[SessionView, ...]:
        async with transaction(self.factory) as session:
            _, _, now = await _require_current(session, current)
            records = await session.scalars(
                select(AuthSession)
                .where(
                    AuthSession.user_id == current.user_id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
                .order_by(AuthSession.created_at.desc(), AuthSession.id)
            )
            return tuple(
                SessionView(
                    record.id,
                    record.created_at,
                    record.last_seen_at,
                    record.expires_at,
                    record.user_agent,
                    record.created_ip,
                    record.id == current.id,
                )
                for record in records
            )

    async def logout(self, value: str | None) -> None:
        if not is_session_token(value):
            return
        assert value is not None
        async with transaction(self.factory) as session:
            owner_id = await session.scalar(
                select(AuthSession.user_id).where(AuthSession.token_hash == digest_token(value))
            )
            if owner_id is None or await _lock_user(session, owner_id) is None:
                return
            now = await database_now(session)
            # Logout remains idempotent for expired/disabled/deleted accounts.
            await session.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == owner_id,
                    AuthSession.token_hash == digest_token(value),
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )

    async def revoke_others(self, current: CurrentSession) -> None:
        async with transaction(self.factory) as session:
            _, _, now = await _require_current(session, current)
            await session.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == current.user_id,
                    AuthSession.id != current.id,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
