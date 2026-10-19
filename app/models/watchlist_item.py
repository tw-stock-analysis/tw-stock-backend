from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.watchlist import Watchlist


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        CheckConstraint("market = 'TWSE'", name="market_allowed"),
        CheckConstraint("stock_code ~ '^[0-9A-Z]{4,6}$'", name="stock_code_format"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        UniqueConstraint(
            "watchlist_id",
            "position",
            name="uq_watchlist_items_watchlist_id_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        {"schema": "app"},
    )

    watchlist_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.watchlists.id", ondelete="CASCADE"), primary_key=True
    )
    market: Mapped[str] = mapped_column(
        String(16), primary_key=True, default="TWSE", server_default="TWSE"
    )
    stock_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    watchlist: Mapped["Watchlist"] = relationship(back_populates="items", lazy="raise")

    @validates("market", "stock_code")
    def normalize_identifier(self, _key: str, value: str) -> str:
        return value.strip().upper()
