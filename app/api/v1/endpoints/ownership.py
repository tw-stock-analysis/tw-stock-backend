from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import OpenDataServiceDependency
from app.schemas.open_data import DataCategory as C
from app.schemas.open_data import DatasetQuery, DatasetRecordsResponse

router = APIRouter(prefix="/ownership", tags=["ownership"])
Input = Annotated[DatasetQuery, Query()]


@router.get("/foreign-holdings", response_model=DatasetRecordsResponse)
async def foreign(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.FOREIGN_HOLDINGS})


@router.get("/institutional-trades", response_model=DatasetRecordsResponse)
async def institutional(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.INSTITUTIONAL_TRADES})


@router.get("/margin-balances", response_model=DatasetRecordsResponse)
async def margin(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.MARGIN_BALANCES})


@router.get("/lending-limits", response_model=DatasetRecordsResponse)
async def lending(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.LENDING_LIMITS})


@router.get("/holding-distributions", response_model=DatasetRecordsResponse)
async def distributions(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.HOLDING_DISTRIBUTIONS})


@router.get("/insider-holdings", response_model=DatasetRecordsResponse)
async def insiders(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.INSIDER_HOLDINGS})


@router.get("/insider-pledges", response_model=DatasetRecordsResponse)
async def pledges(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.INSIDER_PLEDGES})


@router.get("/insider-transfers", response_model=DatasetRecordsResponse)
async def transfers(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.INSIDER_TRANSFERS})
