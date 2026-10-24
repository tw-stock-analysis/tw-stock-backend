"""Actual PostgreSQL ownership, concurrency, limits and user-data persistence."""

import asyncio

import pytest
from sqlalchemy import delete, func, select

from app.models import User, WorkspaceItem
from app.schemas.workspace import NoteDocument, PortfolioDocument
from app.services.errors import ServiceError
from app.services.workspace import WorkspaceService


def note(name="研究"):
    return NoteDocument(
        kind="note", name=name, instrument={"market": "TPEX", "code": "6488"}, body="私人研究"
    )


async def test_owner_scope_versions_duplicates_and_hard_delete_cascade(service_db):
    owner, other = await service_db.user(), await service_db.user()
    service = WorkspaceService(service_db.factory)
    item = await service.save(owner.id, note("Example"))
    assert item.version == 1
    assert (await service.get(owner.id, item.id)).document.body == "私人研究"
    assert await service.list(other.id) == []
    for operation in (
        service.get(other.id, item.id),
        service.save(other.id, note(), item.id, 1),
        service.delete(other.id, item.id, 1),
    ):
        with pytest.raises(ServiceError) as error:
            await operation
        assert error.value.status_code == 404
    with pytest.raises(ServiceError) as error:
        await service.save(owner.id, note("example"))
    assert error.value.code == "WORKSPACE_NAME_EXISTS"
    newer = await service.save(owner.id, note("Updated"), item.id, 1)
    assert newer.version == 2
    with pytest.raises(ServiceError) as error:
        await service.delete(owner.id, item.id, 1)
    assert error.value.code == "WORKSPACE_VERSION_CONFLICT"
    async with service_db.factory.begin() as session:
        await session.execute(delete(User).where(User.id == owner.id))
    async with service_db.factory() as session:
        assert await session.get(WorkspaceItem, item.id) is None


async def test_concurrent_edit_has_one_winner_and_keeps_document_consistent(service_db):
    owner = await service_db.user()
    service = WorkspaceService(service_db.factory)
    item = await service.save(owner.id, note())
    result = await asyncio.gather(
        service.save(owner.id, note("A"), item.id, 1),
        service.save(owner.id, note("B"), item.id, 1),
        return_exceptions=True,
    )
    errors = [r for r in result if isinstance(r, ServiceError)]
    assert len(errors) == 1 and errors[0].code == "WORKSPACE_VERSION_CONFLICT"
    saved = await service.get(owner.id, item.id)
    assert saved.version == 2 and saved.name == saved.document.name


async def test_limit_and_portfolio_invalid_ledger_roll_back(service_db):
    owner = await service_db.user()
    service = WorkspaceService(service_db.factory)
    with pytest.raises(ServiceError):
        await service.save(
            owner.id,
            PortfolioDocument(
                kind="portfolio",
                name="非法帳本",
                ledger=[
                    {
                        "kind": "sell",
                        "code": "2330",
                        "date": "2025-01-02",
                        "quantity": "1",
                        "price": "100",
                    }
                ],
            ),
        )
    async with service_db.factory.begin() as session:
        session.add_all(
            [
                WorkspaceItem(
                    user_id=owner.id,
                    kind="note",
                    name=f"Note {i}",
                    payload=note(f"Note {i}").model_dump(mode="json"),
                )
                for i in range(100)
            ]
        )
    with pytest.raises(ServiceError) as error:
        await service.save(owner.id, note("超出上限"))
    assert error.value.code == "WORKSPACE_LIMIT"
    async with service_db.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(WorkspaceItem)
                .where(WorkspaceItem.user_id == owner.id)
            )
            == 100
        )
