"""Create user-owned watchlists and stock-code items without market data.

Revision ID: 0003_watchlist_tables
Revises: 0002_account_tables
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_watchlist_tables"
down_revision = "0002_account_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "name = btrim(name) AND char_length(name) > 0",
            name=op.f("ck_watchlists_name_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.id"],
            name=op.f("fk_watchlists_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlists")),
        schema="app",
    )
    op.create_index(
        "ix_watchlists_user_id_created_at",
        "watchlists",
        ["user_id", "created_at"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "watchlist_items",
        sa.Column("watchlist_id", sa.Uuid(), nullable=False),
        sa.Column("market", sa.String(length=16), server_default="TWSE", nullable=False),
        sa.Column("stock_code", sa.String(length=6), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("market = 'TWSE'", name=op.f("ck_watchlist_items_market_allowed")),
        sa.CheckConstraint(
            "stock_code ~ '^[0-9A-Z]{4,6}$'", name=op.f("ck_watchlist_items_stock_code_format")
        ),
        sa.CheckConstraint("position >= 0", name=op.f("ck_watchlist_items_position_nonnegative")),
        sa.ForeignKeyConstraint(
            ["watchlist_id"],
            ["app.watchlists.id"],
            name=op.f("fk_watchlist_items_watchlist_id_watchlists"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "watchlist_id", "market", "stock_code", name=op.f("pk_watchlist_items")
        ),
        schema="app",
    )
    op.create_index(
        "ix_watchlist_items_watchlist_id_position",
        "watchlist_items",
        ["watchlist_id", "position"],
        unique=False,
        schema="app",
    )


def downgrade():
    op.drop_index(
        "ix_watchlist_items_watchlist_id_position", table_name="watchlist_items", schema="app"
    )
    op.drop_table("watchlist_items", schema="app")
    op.drop_index("ix_watchlists_user_id_created_at", table_name="watchlists", schema="app")
    op.drop_table("watchlists", schema="app")
