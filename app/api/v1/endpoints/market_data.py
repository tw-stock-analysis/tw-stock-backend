from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import OpenDataServiceDependency
from app.schemas.open_data import DataCategory as C
from app.schemas.open_data import DatasetQuery, DatasetRecordsResponse

router = APIRouter(prefix="/market-data", tags=["market-data"])
Input = Annotated[DatasetQuery, Query()]


@router.get("/odd-lot", response_model=DatasetRecordsResponse)
async def odd_lot(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.ODD_LOT})


@router.get("/block-trading", response_model=DatasetRecordsResponse)
async def block_trading(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.BLOCK_TRADING})


@router.get("/daily-prices", response_model=DatasetRecordsResponse)
async def prices(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.DAILY_PRICES})


@router.get("/day-trading", response_model=DatasetRecordsResponse)
async def day_trading(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.DAY_TRADING})


@router.get("/after-hours", response_model=DatasetRecordsResponse)
async def after_hours(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.AFTER_HOURS})


@router.get("/calendar", response_model=DatasetRecordsResponse)
async def calendar(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.CALENDAR})
