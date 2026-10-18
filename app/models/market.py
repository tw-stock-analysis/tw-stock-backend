from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("market", "ticker"),
        CheckConstraint("market IN ('TWSE', 'TPEX')", name="market"),
        {"schema": "market"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    market: Mapped[str] = mapped_column(String(8))
    ticker: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(100))
    company_name: Mapped[str] = mapped_column(String(200))
    industry_code: Mapped[str | None] = mapped_column(String(8))
    source_date: Mapped[date]
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_version: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict] = mapped_column(JSONB)


class DailyPrice(Base):
    __tablename__ = "daily_prices"
    __table_args__ = (
        CheckConstraint("volume_shares IS NULL OR volume_shares >= 0", name="volume_nonnegative"),
        {"schema": "market"},
    )

    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("market.instruments.id"),
        primary_key=True,
    )
    trade_date: Mapped[date] = mapped_column(primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    high: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    low: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    close: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    volume_shares: Mapped[Decimal | None] = mapped_column(Numeric(24, 0))
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(30, 0))
    transactions: Mapped[int | None]
    change: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_version: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict] = mapped_column(JSONB)


class MarketDataset(Base):
    __tablename__ = "datasets"
    __table_args__ = {"schema": "market"}

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(300))
    data_as_of: Mapped[date]
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_version: Mapped[str] = mapped_column(String(64))
    record_count: Mapped[int]
