from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.auth_deps import CurrentSessionDependency
from app.api.deps import RequestSourceDependency
from app.schemas.analysis import taipei_today
from app.schemas.workspace import (
    DocumentKind,
    PortfolioDocument,
    WorkspaceCreate,
    WorkspaceItemResponse,
    WorkspaceUpdate,
)
from app.services.errors import ServiceError
from app.services.portfolio import value_portfolio
from app.services.workspace import WorkspaceService

router = APIRouter(prefix="/workspace/items", tags=["workspace"])


def service(request: Request) -> WorkspaceService:
    return WorkspaceService(request.app.state.session_factory)


WorkspaceDependency = Annotated[WorkspaceService, Depends(service)]


@router.get("", response_model=list[WorkspaceItemResponse])
async def list_items(
    current: CurrentSessionDependency,
    workspace: WorkspaceDependency,
    kind: DocumentKind | None = None,
):
    return await workspace.list(current.user_id, kind)


@router.get("/{item_id}", response_model=WorkspaceItemResponse)
async def get_item(
    item_id: UUID, current: CurrentSessionDependency, workspace: WorkspaceDependency
):
    return await workspace.get(current.user_id, item_id)


@router.post("", response_model=WorkspaceItemResponse, status_code=201)
async def create_item(
    body: WorkspaceCreate, current: CurrentSessionDependency, workspace: WorkspaceDependency
):
    return await workspace.save(current.user_id, body.document)


@router.put("/{item_id}", response_model=WorkspaceItemResponse)
async def update_item(
    item_id: UUID,
    body: WorkspaceUpdate,
    current: CurrentSessionDependency,
    workspace: WorkspaceDependency,
):
    return await workspace.save(current.user_id, body.document, item_id, body.version)


@router.delete("/{item_id}", status_code=204)
async def delete_item(
    item_id: UUID,
    current: CurrentSessionDependency,
    workspace: WorkspaceDependency,
    version: Annotated[int, Query(ge=1)],
):
    await workspace.delete(current.user_id, item_id, version)
    return Response(status_code=204)


@router.get("/{item_id}/valuation")
async def portfolio_value(
    item_id: UUID,
    current: CurrentSessionDependency,
    workspace: WorkspaceDependency,
    source: RequestSourceDependency,
    as_of: date | None = None,
):
    day = as_of or taipei_today()
    if not date(1994, 10, 1) <= day <= taipei_today():
        raise ServiceError("DATE_INVALID", "請使用可查詢的歷史日期。", 422)
    item = await workspace.get(current.user_id, item_id)
    if not isinstance(item.document, PortfolioDocument):
        raise ServiceError("WORKSPACE_KIND_INVALID", "請選擇投資組合文件。", 422)
    result = await value_portfolio(item.document.ledger, source, day)
    # Recheck account/document ownership after the source calls.
    fresh = await workspace.get(current.user_id, item_id)
    if fresh.version != item.version:
        raise ServiceError(
            "WORKSPACE_VERSION_CONFLICT", "估值期間帳本已更新，請重新載入再查詢。", 409
        )
    return {**result, "item_id": item.id, "version": item.version}
