"""Owner-scoped user inputs with optimistic concurrency; never stores market results."""

from uuid import UUID

from sqlalchemy import func, select

from app.models import WorkspaceItem
from app.schemas.workspace import Document, DocumentKind, PortfolioDocument, WorkspaceItemResponse
from app.services.account_state import SessionFactory, database_now, require_user, transaction
from app.services.errors import ServiceError
from app.services.portfolio import ledger_positions


def view(item: WorkspaceItem) -> WorkspaceItemResponse:
    return WorkspaceItemResponse.model_validate(
        {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "version": item.version,
            "document": item.payload,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
    )


class WorkspaceService:
    def __init__(self, factory: SessionFactory):
        self.factory = factory

    async def list(
        self, user_id: UUID, kind: DocumentKind | None = None
    ) -> list[WorkspaceItemResponse]:
        async with self.factory() as session:
            await require_user(session, user_id)
            query = select(WorkspaceItem).where(WorkspaceItem.user_id == user_id)
            if kind:
                query = query.where(WorkspaceItem.kind == kind)
            return [
                view(r)
                for r in await session.scalars(
                    query.order_by(WorkspaceItem.updated_at.desc(), WorkspaceItem.id)
                )
            ]

    async def get(self, user_id: UUID, item_id: UUID) -> WorkspaceItemResponse:
        async with self.factory() as session:
            await require_user(session, user_id)
            item = await session.scalar(
                select(WorkspaceItem).where(
                    WorkspaceItem.id == item_id, WorkspaceItem.user_id == user_id
                )
            )
            if item is None:
                raise ServiceError("WORKSPACE_NOT_FOUND", "找不到這份研究文件。", 404)
            return view(item)

    async def save(
        self,
        user_id: UUID,
        document: Document,
        item_id: UUID | None = None,
        version: int | None = None,
    ) -> WorkspaceItemResponse:
        if isinstance(document, PortfolioDocument):
            ledger_positions(document.ledger)
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            if item_id:
                item = await session.scalar(
                    select(WorkspaceItem)
                    .where(WorkspaceItem.id == item_id, WorkspaceItem.user_id == user_id)
                    .with_for_update()
                )
                if item is None:
                    raise ServiceError("WORKSPACE_NOT_FOUND", "找不到這份研究文件。", 404)
                if item.version != version:
                    raise ServiceError(
                        "WORKSPACE_VERSION_CONFLICT", "文件已被更新，請重新載入後再編輯。", 409
                    )
                if item.kind != document.kind:
                    raise ServiceError("WORKSPACE_KIND_CONFLICT", "不能變更文件類型。", 422)
                item.version += 1
                item.updated_at = await database_now(session)
            else:
                count = await session.scalar(
                    select(func.count())
                    .select_from(WorkspaceItem)
                    .where(WorkspaceItem.user_id == user_id)
                )
                if count is not None and count >= 100:
                    raise ServiceError("WORKSPACE_LIMIT", "每個帳號最多保存 100 份研究文件。", 409)
                item = WorkspaceItem(user_id=user_id, kind=document.kind)
                session.add(item)
            item.name, item.payload = document.name, document.model_dump(mode="json")
            await session.flush()
            return view(item)

    async def delete(self, user_id: UUID, item_id: UUID, version: int) -> None:
        async with transaction(self.factory) as session:
            await require_user(session, user_id, lock=True)
            item = await session.scalar(
                select(WorkspaceItem)
                .where(WorkspaceItem.id == item_id, WorkspaceItem.user_id == user_id)
                .with_for_update()
            )
            if item is None:
                raise ServiceError("WORKSPACE_NOT_FOUND", "找不到這份研究文件。", 404)
            if item.version != version:
                raise ServiceError(
                    "WORKSPACE_VERSION_CONFLICT", "文件已更新，請重新載入後再刪除。", 409
                )
            await session.delete(item)
