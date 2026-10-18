from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import TwseServiceDependency
from app.schemas.twse import (
    CatalogFilter,
    EmptyQuery,
    TradingCatalogResponse,
    TradingDataset,
    TradingResponse,
)
from app.services.twse_catalog import DATASETS

router = APIRouter(prefix="/twse", tags=["TWSE"])


@router.get("/datasets", response_model=TradingCatalogResponse)
async def catalog(query: Annotated[CatalogFilter, Query()]) -> TradingCatalogResponse:
    return TradingCatalogResponse(
        data=[
            dataset
            for dataset in DATASETS
            if (query.category is None or query.category == dataset.category)
            and (
                not query.q
                or query.q.casefold()
                in (dataset.title + dataset.description + dataset.path).casefold()
            )
        ]
    )


def dataset_endpoint(dataset: TradingDataset):
    async def retrieve(
        service: TwseServiceDependency,
        query: Annotated[EmptyQuery, Query()],
    ) -> TradingResponse:
        return await service.fetch(dataset)

    return retrieve


for dataset in DATASETS:
    router.add_api_route(
        dataset.path,
        dataset_endpoint(dataset),
        methods=["GET"],
        response_model=TradingResponse,
        name=dataset.id,
        operation_id="twse_" + dataset.id,
        summary=dataset.title,
        description=dataset.description + " 每次請求都直接呼叫 TWSE。",
        tags=[dataset.category],
        responses={502: {"description": "TWSE 資料格式無法辨識"}},
    )
