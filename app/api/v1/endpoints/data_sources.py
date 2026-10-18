from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import OpenDataServiceDependency
from app.schemas.open_data import (
    DataCategory,
    DataProvider,
    DatasetCatalogResponse,
    DatasetRecordsResponse,
    RecordQuery,
)

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


@router.get("", response_model=DatasetCatalogResponse)
async def catalog(
    service: OpenDataServiceDependency,
    provider: DataProvider | None = None,
    category: DataCategory | None = None,
) -> DatasetCatalogResponse:
    return await service.catalog(provider, category)


@router.get("/{dataset}/records", response_model=DatasetRecordsResponse)
async def records(
    dataset: str,
    query: Annotated[RecordQuery, Query()],
    service: OpenDataServiceDependency,
) -> DatasetRecordsResponse:
    return await service.records(dataset, query)
