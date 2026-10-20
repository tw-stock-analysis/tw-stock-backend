"""Opaque credentials only. Password checking, cookies and email delivery are caller concerns."""

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuthSession, User, VerificationToken
from app.services.account_state import SessionFactory, database_now, require_user, transaction
from app.services.errors import ServiceError

Purpose = Literal["verify_email", "reset_password"]
IpAddress = IPv4Address | IPv6Address | None


@dataclass(frozen=True)
class IssuedCredential:
    id: UUID
    expires_at: datetime
    value: str = field(repr=False)


@dataclass(frozen=True)
class AuthenticatedSession:
    id: UUID
    user_id: UUID


def digest_token(value: str) -> bytes:
    return sha256(value.encode("utf-8")).digest()


def check_ttl(ttl: timedelta) -> None:
    if ttl <= timedelta(0):
        raise ValueError("Credential TTL must be positive")


def invalid_token() -> ServiceError:
    return ServiceError("TOKEN_INVALID", "憑證無效、已使用或已過期。", 400)


async def consume_verification(
    session: AsyncSession, value: str, purpose: Purpose
) -> tuple[User, datetime]:
    """Private helper: consumption and its account action stay in the caller's transaction."""
    token_hash = digest_token(value)
    user_id = await session.scalar(
        select(VerificationToken.user_id).where(
            VerificationToken.token_hash == token_hash, VerificationToken.purpose == purpose
        )
    )
    if user_id is None:
        raise invalid_token()
    statuses = ("pending_verification", "active") if purpose == "verify_email" else ("active",)
    try:
        user = await require_user(session, user_id, lock=True, statuses=statuses)
    except ServiceError as error:
        raise invalid_token() from error
    now = await database_now(session)
    consumed = await session.scalar(
        update(VerificationToken)
        .where(
            VerificationToken.token_hash == token_hash,
            VerificationToken.purpose == purpose,
            VerificationToken.consumed_at.is_(None),
            VerificationToken.revoked_at.is_(None),
            VerificationToken.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(VerificationToken.id)
    )
    if consumed is None:
        raise invalid_token()
    return user, now


class VerificationService:
    def __init__(self, factory: SessionFactory):
        self.factory = factory

    async def issue(
        self, user_id: UUID, purpose: Purpose, ttl: timedelta, *, created_ip: IpAddress = None
    ) -> IssuedCredential:
        if purpose not in ("verify_email", "reset_password"):
            raise ValueError("Unsupported verification purpose")
        check_ttl(ttl)
        async with transaction(self.factory) as session:
            statuses = (
                ("pending_verification", "active") if purpose == "verify_email" else ("active",)
            )
            user = await require_user(session, user_id, lock=True, statuses=statuses)
            if purpose == "verify_email" and user.email_verified_at is not None:
                raise ServiceError("EMAIL_ALREADY_VERIFIED", "Email 已完成驗證。", 409)
            now = await database_now(session)
            await session.execute(
                update(VerificationToken)
                .where(
                    VerificationToken.user_id == user_id,
                    VerificationToken.purpose == purpose,
                    VerificationToken.consumed_at.is_(None),
                    VerificationToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            value = secrets.token_urlsafe(32)
            record = VerificationToken(
                user_id=user_id,
                purpose=purpose,
                token_hash=digest_token(value),
                expires_at=now + ttl,
                created_ip=created_ip,
            )
            session.add(record)
            await session.flush()
            result = IssuedCredential(record.id, record.expires_at, value)
        return result


class SessionService:
    def __init__(self, factory: SessionFactory):
        self.factory = factory

    async def issue(
        self,
        user_id: UUID,
        ttl: timedelta,
        *,
        user_agent: str | None = None,
        created_ip: IpAddress = None,
    ) -> IssuedCredential:
        """Caller must authenticate credentials first; user_id alone is not a login API."""
        check_ttl(ttl)
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            now = await database_now(session)
            value = secrets.token_urlsafe(32)
            record = AuthSession(
                user_id=user_id,
                token_hash=digest_token(value),
                expires_at=now + ttl,
                user_agent=user_agent,
                created_ip=created_ip,
            )
            session.add(record)
            await session.flush()
            result = IssuedCredential(record.id, record.expires_at, value)
        return result

    async def authenticate(self, value: str) -> AuthenticatedSession:
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(AuthSession.id, AuthSession.user_id)
                    .join(User, User.id == AuthSession.user_id)
                    .where(
                        AuthSession.token_hash == digest_token(value),
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > func.clock_timestamp(),
                        User.deleted_at.is_(None),
                        User.status == "active",
                    )
                )
            ).one_or_none()
            if row is None:
                raise ServiceError("SESSION_INVALID", "登入憑證無效或已過期。", 401)
            return AuthenticatedSession(row.id, row.user_id)

    async def revoke(self, user_id: UUID, session_id: UUID) -> None:
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            record = await session.scalar(
                select(AuthSession).where(
                    AuthSession.id == session_id, AuthSession.user_id == user_id
                )
            )
            if record is None:
                raise ServiceError("SESSION_NOT_FOUND", "找不到登入紀錄。", 404)
            if record.revoked_at is None:
                record.revoked_at = await database_now(session)

    async def revoke_others(self, user_id: UUID, current_session_id: UUID) -> None:
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            now = await database_now(session)
            current = await session.scalar(
                select(AuthSession.id).where(
                    AuthSession.id == current_session_id,
                    AuthSession.user_id == user_id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
            )
            if current is None:
                raise ServiceError("SESSION_INVALID", "登入憑證無效或已過期。", 401)
            await session.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == user_id,
                    AuthSession.id != current_session_id,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
