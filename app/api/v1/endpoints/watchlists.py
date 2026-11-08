from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.auth_deps import CurrentSessionDependency
from app.api.deps import StockServiceDependency
from app.schemas.watchlists import (
    WatchlistAdd,
    WatchlistName,
    WatchlistOrder,
    WatchlistResponse,
    WatchlistsResponse,
)
from app.services.watchlists import WatchlistService

router = APIRouter(prefix="/watchlists", tags=["personal watchlists"])


def service(request: Request, stocks: StockServiceDependency) -> WatchlistService:
    return WatchlistService(request.app.state.session_factory, stocks)


WatchlistsDependency = Annotated[WatchlistService, Depends(service)]


@router.get("", response_model=WatchlistsResponse)
async def lists(current: CurrentSessionDependency, watchlists: WatchlistsDependency):
    return {"data": await watchlists.list(current.user_id)}


@router.post("", response_model=WatchlistResponse, status_code=201)
async def create(
    body: WatchlistName, current: CurrentSessionDependency, watchlists: WatchlistsDependency
):
    item_id = await watchlists.create(current.user_id, body.name)
    return await watchlists.get(current.user_id, item_id)


@router.get("/{list_id}", response_model=WatchlistResponse)
async def get(list_id: UUID, current: CurrentSessionDependency, watchlists: WatchlistsDependency):
    return await watchlists.get(current.user_id, list_id)


@router.put("/{list_id}", response_model=WatchlistResponse)
async def rename(
    list_id: UUID,
    body: WatchlistName,
    current: CurrentSessionDependency,
    watchlists: WatchlistsDependency,
):
    await watchlists.rename(current.user_id, list_id, body.name)
    return await watchlists.get(current.user_id, list_id)


@router.delete("/{list_id}", status_code=204)
async def delete(
    list_id: UUID, current: CurrentSessionDependency, watchlists: WatchlistsDependency
):
    await watchlists.delete(current.user_id, list_id)
    return Response(status_code=204)


@router.post("/{list_id}/items", response_model=WatchlistResponse)
async def add(
    list_id: UUID,
    body: WatchlistAdd,
    current: CurrentSessionDependency,
    watchlists: WatchlistsDependency,
):
    await watchlists.add_item(current.user_id, list_id, body.code)
    return await watchlists.get(current.user_id, list_id)


@router.delete("/{list_id}/items/{code}", response_model=WatchlistResponse)
async def remove(
    list_id: UUID, code: str, current: CurrentSessionDependency, watchlists: WatchlistsDependency
):
    await watchlists.remove_item(current.user_id, list_id, code)
    return await watchlists.get(current.user_id, list_id)


@router.put("/{list_id}/order", response_model=WatchlistResponse)
async def reorder(
    list_id: UUID,
    body: WatchlistOrder,
    current: CurrentSessionDependency,
    watchlists: WatchlistsDependency,
):
    await watchlists.reorder(current.user_id, list_id, body.codes)
    return await watchlists.get(current.user_id, list_id)
