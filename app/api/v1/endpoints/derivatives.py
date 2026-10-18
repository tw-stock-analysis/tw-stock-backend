from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import OpenDataServiceDependency
from app.schemas.open_data import DataCategory as C
from app.schemas.open_data import DatasetQuery, DatasetRecordsResponse

router = APIRouter(prefix="/derivatives", tags=["derivatives"])
Input = Annotated[DatasetQuery, Query()]


@router.get("/daily-prices", response_model=DatasetRecordsResponse)
async def prices(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.FUTURES_PRICES, C.OPTIONS_PRICES})


@router.get("/institutional-trades", response_model=DatasetRecordsResponse)
async def institutional(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(
        query.dataset, query, {C.FUTURES_INSTITUTIONAL, C.OPTIONS_INSTITUTIONAL}
    )


@router.get("/large-traders", response_model=DatasetRecordsResponse)
async def large_traders(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(
        query.dataset, query, {C.FUTURES_LARGE_TRADERS, C.OPTIONS_LARGE_TRADERS}
    )


@router.get("/put-call-ratio", response_model=DatasetRecordsResponse)
async def put_call(query: Input, service: OpenDataServiceDependency) -> DatasetRecordsResponse:
    return await service.records(query.dataset, query, {C.PUT_CALL_RATIO})
