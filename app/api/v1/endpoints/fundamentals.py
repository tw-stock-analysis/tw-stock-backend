from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import OpenDataServiceDependency
from app.schemas.open_data import DataCategory as C
from app.schemas.open_data import DatasetQuery, DatasetRecordsResponse

router = APIRouter(prefix="/fundamentals", tags=["fundamentals"])
Input = Annotated[DatasetQuery, Query()]


@router.get("/companies", response_model=DatasetRecordsResponse)
async def companies(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.COMPANIES})


@router.get("/monthly-revenue", response_model=DatasetRecordsResponse)
async def revenue(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.MONTHLY_REVENUE})


@router.get("/financial-statements", response_model=DatasetRecordsResponse)
async def statements(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.INCOME_STATEMENTS, C.BALANCE_SHEETS})


@router.get("/valuations", response_model=DatasetRecordsResponse)
async def valuations(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.VALUATIONS})


@router.get("/dividends", response_model=DatasetRecordsResponse)
async def dividends(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.DIVIDENDS})


@router.get("/corporate-actions", response_model=DatasetRecordsResponse)
async def actions(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.CORPORATE_ACTIONS})


@router.get("/announcements", response_model=DatasetRecordsResponse)
async def announcements(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.ANNOUNCEMENTS})
