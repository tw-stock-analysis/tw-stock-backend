"""Create the three account tables without market-data storage.

Revision ID: 0002_account_tables
Revises: 0001_create_schemas
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_account_tables"
down_revision = "0001_create_schemas"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default="pending_verification", nullable=False
        ),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_verification', 'active', 'disabled')",
            name=op.f("ck_users_status_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(password_hash) > 0", name=op.f("ck_users_password_hash_not_empty")
        ),
        sa.CheckConstraint(
            "email = lower(btrim(email)) AND char_length(email) > 0",
            name=op.f("ck_users_email_normalized"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        schema="app",
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_ip", postgresql.INET(), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_auth_sessions_expiry_after_creation")
        ),
        sa.CheckConstraint(
            "last_seen_at >= created_at", name=op.f("ck_auth_sessions_last_seen_after_creation")
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_auth_sessions_token_hash_sha256")
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_auth_sessions_revocation_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
        schema="app",
    )
    op.create_index(
        op.f("ix_auth_sessions_expires_at"),
        "auth_sessions",
        ["expires_at"],
        unique=False,
        schema="app",
    )
    op.create_index(
        op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"], unique=False, schema="app"
    )
    op.create_table(
        "verification_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_ip", postgresql.INET(), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name=op.f("ck_verification_tokens_purpose_allowed"),
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name=op.f("ck_verification_tokens_consumption_after_creation"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_verification_tokens_expiry_after_creation")
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_verification_tokens_token_hash_sha256")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.id"],
            name=op.f("fk_verification_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_verification_tokens_token_hash")),
        schema="app",
    )
    op.create_index(
        op.f("ix_verification_tokens_expires_at"),
        "verification_tokens",
        ["expires_at"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "ix_verification_tokens_user_id_purpose",
        "verification_tokens",
        ["user_id", "purpose"],
        unique=False,
        schema="app",
    )


def downgrade():
    op.drop_index(
        "ix_verification_tokens_user_id_purpose", table_name="verification_tokens", schema="app"
    )
    op.drop_index(
        op.f("ix_verification_tokens_expires_at"), table_name="verification_tokens", schema="app"
    )
    op.drop_table("verification_tokens", schema="app")
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions", schema="app")
    op.drop_index(op.f("ix_auth_sessions_expires_at"), table_name="auth_sessions", schema="app")
    op.drop_table("auth_sessions", schema="app")
    op.drop_table("users", schema="app")
