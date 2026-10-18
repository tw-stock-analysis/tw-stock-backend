from datetime import date, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OpenDataSnapshot(Base):
    __tablename__ = "open_data_snapshots"
    __table_args__ = {"schema": "market"}

    dataset: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(500))
    data_as_of: Mapped[date | None]
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_version: Mapped[str] = mapped_column(String(64))
    record_count: Mapped[int]


class OpenDataRecord(Base):
    __tablename__ = "open_data_records"
    __table_args__ = (
        UniqueConstraint("dataset", "ordinal"),
        Index("ix_open_data_dataset_symbol_date", "dataset", "symbol", "record_date"),
        {"schema": "market"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(
        ForeignKey("market.open_data_snapshots.dataset", ondelete="CASCADE")
    )
    ordinal: Mapped[int]
    symbol: Mapped[str | None] = mapped_column(String(80))
    record_date: Mapped[date | None]
    record_date_field: Mapped[str | None] = mapped_column(String(300))
    fields: Mapped[dict] = mapped_column(JSONB)
