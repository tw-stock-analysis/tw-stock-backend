"""Private user research documents and user-entered ledgers; no market records."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_workspace"
down_revision = "0004_account_rules"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
            name=op.f("ck_workspace_items_name_not_blank"),
        ),
        sa.CheckConstraint(
            "kind IN ('screen','note','chart','comparison','portfolio')",
            name=op.f("ck_workspace_items_kind_allowed"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_workspace_items_version_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.id"],
            ondelete="CASCADE",
            name=op.f("fk_workspace_items_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_items")),
        schema="app",
    )
    op.create_index(
        "uq_workspace_items_user_kind_name",
        "workspace_items",
        ["user_id", "kind", sa.text("lower(name)")],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_workspace_items_user_id_updated_at",
        "workspace_items",
        ["user_id", "updated_at"],
        schema="app",
    )


def downgrade():
    op.drop_table("workspace_items", schema="app")
