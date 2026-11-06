"""Shared security identity metadata only; no prices, factors or market snapshots."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SecurityIdentity(Base):
    __tablename__ = "security_identities"
    __table_args__ = (
        CheckConstraint("instrument_type IN ('ordinary_share','etf','other')", name="type_allowed"),
        CheckConstraint(
            "issuer_ubn IS NULL OR issuer_ubn ~ '^[0-9]{8}$'", name="issuer_ubn_format"
        ),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    registry_key: Mapped[str] = mapped_column(String(160), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    instrument_type: Mapped[str] = mapped_column(String(32))
    issuer_ubn: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SecurityListing(Base):
    __tablename__ = "security_listings"
    __table_args__ = (
        CheckConstraint("market IN ('TWSE','TPEX','EMERGING')", name="market_allowed"),
        CheckConstraint("code ~ '^[0-9A-Z]{4,6}$'", name="code_format"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_interval"),
        CheckConstraint(
            "(valid_to IS NULL) = (end_evidence IS NULL)", name="end_evidence_required"
        ),
        Index(
            "uq_security_listings_identity_market_code_start",
            "security_id",
            "market",
            "code",
            "valid_from",
            unique=True,
        ),
        Index("ix_security_listings_market_code_dates", "market", "code", "valid_from", "valid_to"),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.security_identities.id", ondelete="CASCADE")
    )
    market: Mapped[str] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(6))
    name: Mapped[str] = mapped_column(String(200))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    start_evidence: Mapped[dict] = mapped_column(JSONB)
    end_evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


class SecurityIdentifier(Base):
    __tablename__ = "security_identifiers"
    __table_args__ = (
        CheckConstraint("kind IN ('isin','name')", name="kind_allowed"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_interval"),
        CheckConstraint(
            "(valid_to IS NULL) = (end_evidence IS NULL)", name="end_evidence_required"
        ),
        Index(
            "uq_security_identifiers_identity_kind_value_start",
            "security_id",
            "kind",
            "value",
            "valid_from",
            unique=True,
        ),
        Index("ix_security_identifiers_kind_value", "kind", "value"),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.security_identities.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(200))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    evidence: Mapped[dict] = mapped_column(JSONB)

    end_evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
