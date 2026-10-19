"""Shared transaction and lock order: user first, then list or credential rows."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuthSession, User, VerificationToken
from app.services.errors import ServiceError

SessionFactory = async_sessionmaker[AsyncSession]


@asynccontextmanager
async def transaction(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    try:
        async with factory.begin() as session:
            yield session
    except IntegrityError as error:
        conflicts = {
            "uq_users_active_email": ("EMAIL_IN_USE", "此 Email 已有未刪除的帳號。"),
            "uq_watchlists_user_id_name": ("WATCHLIST_NAME_EXISTS", "已有同名自選清單。"),
            "uq_watchlist_items_watchlist_id_position": ("POSITION_CONFLICT", "清單排序衝突。"),
        }
        cause: BaseException | None = error.orig
        while cause is not None:
            name = getattr(cause, "constraint_name", None)
            if name in conflicts:
                code, message = conflicts[name]
                raise ServiceError(code, message, 409) from error
            cause = cause.__cause__
        raise


async def require_user(
    session: AsyncSession,
    user_id: UUID,
    *,
    lock: bool = False,
    statuses: tuple[str, ...] = ("active",),
    allow_deleted: bool = False,
) -> User:
    query = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    user = await session.scalar(query)
    if user is None or (user.deleted_at is not None and not allow_deleted):
        raise ServiceError("ACCOUNT_UNAVAILABLE", "帳號無法使用。", 403)
    if user.status not in statuses:
        raise ServiceError("ACCOUNT_UNAVAILABLE", "帳號無法使用。", 403)
    return user


async def database_now(session: AsyncSession) -> datetime:
    # Unlike now(), this does not freeze at transaction start while waiting for a lock.
    value = await session.scalar(select(func.clock_timestamp()))
    assert isinstance(value, datetime)
    return value


async def revoke_credentials(session: AsyncSession, user_id: UUID, now: datetime) -> None:
    await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.execute(
        update(VerificationToken)
        .where(
            VerificationToken.user_id == user_id,
            VerificationToken.consumed_at.is_(None),
            VerificationToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
