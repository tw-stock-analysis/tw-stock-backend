"""Private simulation inputs and orders, separate from real portfolio ledgers."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    __table_args__ = (
        CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 100", name="name_valid"
        ),
        CheckConstraint("initial_cash > 0 AND initial_cash <= 1000000000", name="cash_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("uq_paper_accounts_user_name", "user_id", text("lower(name)"), unique=True),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app.users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    orders: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
