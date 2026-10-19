"""Run with: uv run python -m app.services.auth_cleanup [--dry-run]."""

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import AuthSession, VerificationToken
from app.services.account_state import SessionFactory, database_now


@dataclass(frozen=True)
class CleanupResult:
    sessions: int
    verification_tokens: int
    dry_run: bool


async def cleanup_credentials(
    factory: SessionFactory,
    *,
    retention_days: int = 30,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> CleanupResult:
    if retention_days < 0 or not 1 <= batch_size <= 10000:
        raise ValueError("retention_days must be nonnegative; batch_size must be 1–10000")
    async with factory() as session:
        cutoff = await database_now(session) - timedelta(days=retention_days)
    totals = []
    for model in (AuthSession, VerificationToken):
        conditions = [model.expires_at < cutoff, model.revoked_at < cutoff]
        if model is VerificationToken:
            conditions.append(VerificationToken.consumed_at < cutoff)
        ended = or_(*conditions)
        count = 0
        if dry_run:
            async with factory() as session:
                count = (
                    await session.scalar(select(func.count()).select_from(model).where(ended)) or 0
                )
        else:
            while True:
                async with factory.begin() as session:
                    candidates = (
                        select(model.id)
                        .where(ended)
                        .order_by(model.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                    removed = list(
                        await session.scalars(
                            delete(model).where(model.id.in_(candidates)).returning(model.id)
                        )
                    )
                    count += len(removed)
                if len(removed) < batch_size:
                    break
        totals.append(count)
    return CleanupResult(totals[0], totals[1], dry_run)


async def run(retention_days: int, batch_size: int, dry_run: bool) -> CleanupResult:
    engine = create_async_engine(get_settings().sqlalchemy_url())
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return await cleanup_credentials(
            factory, retention_days=retention_days, batch_size=batch_size, dry_run=dry_run
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean credentials ended over 30 days ago")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.retention_days < 0 or not 1 <= args.batch_size <= 10000:
        parser.error("retention-days must be nonnegative; batch-size must be 1–10000")
    try:
        result = asyncio.run(run(args.retention_days, args.batch_size, args.dry_run))
    except Exception:
        # Database URLs, credential values and SQL parameters must not enter command output.
        parser.exit(1, "Credential cleanup failed; check database connectivity and migrations.\n")
    print(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
