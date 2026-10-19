from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.watchlist_item import WatchlistItem


class Watchlist(Base):
    __tablename__ = "watchlists"
    __table_args__ = (
        CheckConstraint("name = btrim(name) AND char_length(name) > 0", name="name_not_blank"),
        Index("ix_watchlists_user_id_created_at", "user_id", "created_at"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app.users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="watchlists", lazy="raise")
    items: Mapped[list["WatchlistItem"]] = relationship(
        back_populates="watchlist",
        cascade="save-update, merge, delete, delete-orphan",
        passive_deletes=True,
        lazy="raise",
        order_by="(WatchlistItem.position, WatchlistItem.market, WatchlistItem.stock_code)",
    )

    @validates("name")
    def normalize_name(self, _key: str, value: str) -> str:
        return value.strip()
