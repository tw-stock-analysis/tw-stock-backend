"""Read-only dataset services. Queries never fetch remote data."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.open_data import OpenDataRecord, OpenDataSnapshot
from app.schemas.common import Pagination
from app.schemas.open_data import (
    DataCategory,
    DataProvider,
    DatasetCatalogResponse,
    DatasetInfo,
    DatasetRecordsResponse,
    RecordQuery,
    SourceRecord,
)
from app.services.data_catalog import DATASETS, get_dataset
from app.services.errors import ServiceError


class OpenDataService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def catalog(
        self,
        provider: DataProvider | None = None,
        category: DataCategory | None = None,
    ) -> DatasetCatalogResponse:
        snapshots = {
            row.dataset: row for row in (await self.session.scalars(select(OpenDataSnapshot))).all()
        }
        data = []
        for dataset in DATASETS.values():
            if (
                provider
                and dataset.provider != provider
                or category
                and dataset.category != category
            ):
                continue
            row = snapshots.get(dataset.key)
            meta = dataset.metadata()
            if row:
                meta = meta.model_copy(
                    update={
                        "data_as_of": row.data_as_of,
                        "source_url": row.source_url,
                        "retrieved_at": row.retrieved_at,
                        "data_version": row.data_version,
                    }
                )
            data.append(
                DatasetInfo(
                    meta=meta,
                    status="available" if row else "not_loaded",
                    record_count=row.record_count if row else 0,
                    industry=dataset.industry,
                    symbol_filter_supported=True if dataset.require_symbol else None,
                )
            )
        return DatasetCatalogResponse(data=data)

    async def records(
        self,
        key: str,
        query: RecordQuery,
        categories: set[DataCategory] | None = None,
    ) -> DatasetRecordsResponse:
        dataset = get_dataset(key)
        if categories is not None and dataset.category not in categories:
            raise ServiceError(
                "DATASET_CATEGORY_MISMATCH", "Dataset does not belong to this endpoint.", 422
            )
        snapshot = await self.session.get(OpenDataSnapshot, key)
        if snapshot is None:
            raise ServiceError(
                "DATASET_NOT_LOADED", "Run the public data import for this dataset.", 503
            )
        if query.symbol and not dataset.require_symbol:
            has_symbols = await self.session.scalar(
                select(OpenDataRecord.id)
                .where(
                    OpenDataRecord.dataset == key,
                    OpenDataRecord.symbol.is_not(None),
                )
                .limit(1)
            )
            if has_symbols is None:
                raise ServiceError(
                    "SYMBOL_FILTER_UNSUPPORTED",
                    "This source snapshot has no instrument identifiers.",
                    422,
                )
        conditions = [OpenDataRecord.dataset == key]
        if query.symbol:
            conditions.append(OpenDataRecord.symbol == query.symbol)
        if query.date_from:
            conditions.append(OpenDataRecord.record_date >= query.date_from)
        if query.date_to:
            conditions.append(OpenDataRecord.record_date <= query.date_to)
        total = await self.session.scalar(
            select(func.count()).select_from(OpenDataRecord).where(*conditions)
        )
        rows = (
            await self.session.scalars(
                select(OpenDataRecord)
                .where(*conditions)
                .order_by(OpenDataRecord.record_date.desc().nulls_last(), OpenDataRecord.ordinal)
                .offset((query.page - 1) * query.limit)
                .limit(query.limit)
            )
        ).all()
        return DatasetRecordsResponse(
            data=[
                SourceRecord(
                    symbol=r.symbol,
                    record_date=r.record_date,
                    record_date_field=r.record_date_field,
                    fields=r.fields,
                )
                for r in rows
            ],
            pagination=Pagination(page=query.page, limit=query.limit, total=total or 0),
            meta=dataset.metadata().model_copy(
                update={
                    "data_as_of": snapshot.data_as_of,
                    "source_url": snapshot.source_url,
                    "retrieved_at": snapshot.retrieved_at,
                    "data_version": snapshot.data_version,
                }
            ),
        )
