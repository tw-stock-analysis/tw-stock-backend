"""Store official instrument and daily price snapshots."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_market_data"
down_revision = "0001_create_schemas"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instruments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("ticker", sa.String(12), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("industry_code", sa.String(8)),
        sa.Column("source_date", sa.Date(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_version", sa.String(64), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("market", "ticker"),
        sa.CheckConstraint("market IN ('TWSE', 'TPEX')", name="market"),
        schema="market",
    )
    op.create_table(
        "daily_prices",
        sa.Column(
            "instrument_id", sa.Uuid(), sa.ForeignKey("market.instruments.id"), primary_key=True
        ),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        *(sa.Column(name, sa.Numeric(24, 6)) for name in ("open", "high", "low", "close")),
        sa.Column("volume_shares", sa.Numeric(24, 0)),
        sa.Column("turnover", sa.Numeric(30, 0)),
        sa.Column("transactions", sa.Integer()),
        sa.Column("change", sa.Numeric(24, 6)),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_version", sa.String(64), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "volume_shares IS NULL OR volume_shares >= 0", name="volume_nonnegative"
        ),
        schema="market",
    )
    op.create_table(
        "datasets",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("source_url", sa.String(300), nullable=False),
        sa.Column("data_as_of", sa.Date(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_version", sa.String(64), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        schema="market",
    )


def downgrade():
    op.drop_table("datasets", schema="market")
    op.drop_table("daily_prices", schema="market")
    op.drop_table("instruments", schema="market")
