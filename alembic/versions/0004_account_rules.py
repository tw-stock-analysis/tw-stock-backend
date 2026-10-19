"""Enforce the approved account, watchlist and verification-token rules.

Revision ID: 0004_account_rules
Revises: 0003_watchlist_tables
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_account_rules"
down_revision = "0003_watchlist_tables"
branch_labels = None
depends_on = None


def upgrade():
    # Fail transactionally instead of silently renaming private user data.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM app.watchlists
                GROUP BY user_id, lower(name) HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'Resolve duplicate watchlist names before upgrading to 0004';
            END IF;
            IF EXISTS (
                SELECT 1 FROM app.watchlists GROUP BY user_id HAVING count(*) > 10
            ) OR EXISTS (
                SELECT 1 FROM app.watchlist_items GROUP BY watchlist_id HAVING count(*) > 100
            ) THEN
                RAISE EXCEPTION 'Resolve watchlist capacity violations before upgrading to 0004';
            END IF;
        END $$
    """)
    op.drop_constraint("uq_users_email", "users", schema="app", type_="unique")
    op.create_index(
        "uq_users_active_email",
        "users",
        [sa.text("lower(email)")],
        unique=True,
        schema="app",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_watchlists_user_id_name",
        "watchlists",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        schema="app",
    )
    # Preserve the old stable order while removing ties and gaps.
    op.execute("""
        WITH ordered AS (
            SELECT watchlist_id, market, stock_code,
                row_number() OVER (
                    PARTITION BY watchlist_id ORDER BY position, market, stock_code
                ) - 1 AS new_position
            FROM app.watchlist_items
        )
        UPDATE app.watchlist_items AS item SET position = ordered.new_position
        FROM ordered WHERE item.watchlist_id = ordered.watchlist_id
            AND item.market = ordered.market AND item.stock_code = ordered.stock_code
    """)
    op.drop_index("ix_watchlist_items_watchlist_id_position", "watchlist_items", schema="app")
    op.alter_column("watchlist_items", "position", server_default=None, schema="app")
    op.create_unique_constraint(
        "uq_watchlist_items_watchlist_id_position",
        "watchlist_items",
        ["watchlist_id", "position"],
        schema="app",
        deferrable=True,
        initially="DEFERRED",
    )
    op.add_column(
        "verification_tokens", sa.Column("revoked_at", sa.DateTime(timezone=True)), schema="app"
    )
    op.create_check_constraint(
        op.f("ck_verification_tokens_revocation_after_creation"),
        "verification_tokens",
        "revoked_at IS NULL OR revoked_at >= created_at",
        schema="app",
    )
    op.execute("""
        UPDATE app.auth_sessions AS session
        SET revoked_at = greatest(session.created_at, clock_timestamp())
        FROM app.users AS account
        WHERE account.id = session.user_id AND account.deleted_at IS NOT NULL
            AND session.revoked_at IS NULL
    """)
    op.execute("""
        UPDATE app.verification_tokens AS token
        SET revoked_at = greatest(token.created_at, clock_timestamp())
        FROM app.users AS account
        WHERE account.id = token.user_id AND token.consumed_at IS NULL
            AND token.revoked_at IS NULL
            AND (account.deleted_at IS NOT NULL OR token.expires_at <= clock_timestamp())
    """)
    op.execute("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY user_id, purpose ORDER BY created_at DESC, id DESC
            ) AS rank
            FROM app.verification_tokens
            WHERE consumed_at IS NULL AND revoked_at IS NULL
        )
        UPDATE app.verification_tokens AS token
        SET revoked_at = greatest(token.created_at, clock_timestamp())
        FROM ranked WHERE ranked.id = token.id AND ranked.rank > 1
    """)
    op.create_index(
        "uq_verification_tokens_pending",
        "verification_tokens",
        ["user_id", "purpose"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("consumed_at IS NULL AND revoked_at IS NULL"),
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM app.users GROUP BY email HAVING count(*) > 1) THEN
                RAISE EXCEPTION 'Email reuse prevents downgrade; resolve duplicate accounts first';
            END IF;
        END $$
    """)
    op.drop_index("uq_verification_tokens_pending", "verification_tokens", schema="app")
    # Older code understands consumed_at, but has no revoked_at. Keep tokens unusable.
    op.execute("""
        UPDATE app.verification_tokens SET consumed_at = revoked_at
        WHERE consumed_at IS NULL AND revoked_at IS NOT NULL
    """)
    op.drop_constraint(
        op.f("ck_verification_tokens_revocation_after_creation"),
        "verification_tokens",
        schema="app",
        type_="check",
    )
    op.drop_column("verification_tokens", "revoked_at", schema="app")
    op.drop_constraint(
        "uq_watchlist_items_watchlist_id_position", "watchlist_items", schema="app", type_="unique"
    )
    op.alter_column("watchlist_items", "position", server_default="0", schema="app")
    op.create_index(
        "ix_watchlist_items_watchlist_id_position",
        "watchlist_items",
        ["watchlist_id", "position"],
        schema="app",
    )
    op.drop_index("uq_watchlists_user_id_name", "watchlists", schema="app")
    op.drop_index("uq_users_active_email", "users", schema="app")
    op.create_unique_constraint("uq_users_email", "users", ["email"], schema="app")
