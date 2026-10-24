from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkspaceItem(Base):
    __tablename__ = "workspace_items"
    __table_args__ = (
        CheckConstraint("name = btrim(name) AND char_length(name) > 0", name="name_not_blank"),
        CheckConstraint(
            "kind IN ('screen','note','chart','comparison','portfolio')", name="kind_allowed"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        Index(
            "uq_workspace_items_user_kind_name", "user_id", "kind", text("lower(name)"), unique=True
        ),
        Index("ix_workspace_items_user_id_updated_at", "user_id", "updated_at"),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app.users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
