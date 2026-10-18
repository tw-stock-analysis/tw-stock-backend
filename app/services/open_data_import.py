"""Explicit public dataset importer: python -m app.services.open_data_import --help."""

import argparse
import asyncio
import hashlib

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.open_data import OpenDataRecord, OpenDataSnapshot
from app.services.data_catalog import DATASETS, get_dataset
from app.services.errors import ServiceError
from app.services.providers.open_data import OpenDataProvider, SourceSnapshot


async def publish_dataset(session: AsyncSession, snapshot: SourceSnapshot) -> None:
    get_dataset(snapshot.dataset)
    lock = int.from_bytes(hashlib.sha256(snapshot.dataset.encode()).digest()[:8], signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    previous = await session.scalar(
        select(OpenDataSnapshot)
        .where(OpenDataSnapshot.dataset == snapshot.dataset)
        .execution_options(populate_existing=True)
    )
    if previous:
        if snapshot.retrieved_at < previous.retrieved_at or (
            snapshot.data_as_of
            and previous.data_as_of
            and snapshot.data_as_of < previous.data_as_of
        ):
            raise ServiceError(
                "SOURCE_DATE_REGRESSION", "Older snapshots cannot replace data.", 503
            )
    statement = insert(OpenDataSnapshot).values(
        dataset=snapshot.dataset,
        source_url=snapshot.source_url,
        data_as_of=snapshot.data_as_of,
        retrieved_at=snapshot.retrieved_at,
        data_version=snapshot.data_version,
        record_count=len(snapshot.records),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[OpenDataSnapshot.dataset],
            set_={
                k: getattr(statement.excluded, k)
                for k in (
                    "source_url",
                    "data_as_of",
                    "retrieved_at",
                    "data_version",
                    "record_count",
                )
            },
        )
    )
    await session.execute(delete(OpenDataRecord).where(OpenDataRecord.dataset == snapshot.dataset))
    for start in range(0, len(snapshot.records), 500):
        await session.execute(
            insert(OpenDataRecord),
            [
                {"dataset": snapshot.dataset, "ordinal": start + offset, **record.model_dump()}
                for offset, record in enumerate(snapshot.records[start : start + 500])
            ],
        )
    # Caller commits. Any error preserves the entire previously published dataset.


async def run_import(keys: list[str]) -> bool:
    engine = create_async_engine(get_settings().sqlalchemy_url())
    failed = False
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            provider = OpenDataProvider(client)
            for index, key in enumerate(keys):
                if index:
                    await asyncio.sleep(1)
                try:
                    snapshot = await provider.fetch(get_dataset(key))
                    async with async_sessionmaker(engine)() as session, session.begin():
                        await publish_dataset(session, snapshot)
                    print(f"{key}: imported {len(snapshot.records)} records")
                except ServiceError as error:
                    failed = True
                    print(f"{key}: {error.code}: {error.message}")
                except (SQLAlchemyError, OSError, TimeoutError):
                    failed = True
                    print(f"{key}: DEPENDENCY_UNAVAILABLE: import failed")
    finally:
        await engine.dispose()
    return not failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--dataset", action="append", choices=sorted(DATASETS))
    choice.add_argument(
        "--all", action="store_true", help="Import all official datasets sequentially"
    )
    choice.add_argument(
        "--list", action="store_true", help="List source URLs without network/DB access"
    )
    args = parser.parse_args()
    if args.list:
        for key, dataset in DATASETS.items():
            print(f"{key}\t{dataset.category.value}\t{dataset.url}")
        return
    keys = list(DATASETS) if args.all else list(dict.fromkeys(args.dataset))
    raise SystemExit(0 if asyncio.run(run_import(keys)) else 1)


if __name__ == "__main__":
    main()
