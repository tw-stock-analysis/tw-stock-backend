"""Publish validated official source snapshots and records atomically."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_open_data"
down_revision = "0002_market_data"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "open_data_snapshots",
        sa.Column("dataset", sa.String(80), primary_key=True),
        sa.Column("source_url", sa.String(500), nullable=False),
        sa.Column("data_as_of", sa.Date()),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_version", sa.String(64), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        schema="market",
    )
    op.create_table(
        "open_data_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset",
            sa.String(80),
            sa.ForeignKey("market.open_data_snapshots.dataset", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(80)),
        sa.Column("record_date", sa.Date()),
        sa.Column("record_date_field", sa.String(300)),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("dataset", "ordinal"),
        schema="market",
    )
    op.create_index(
        "ix_open_data_dataset_symbol_date",
        "open_data_records",
        ["dataset", "symbol", "record_date"],
        schema="market",
    )


def downgrade():
    op.drop_table("open_data_records", schema="market")
    op.drop_table("open_data_snapshots", schema="market")
