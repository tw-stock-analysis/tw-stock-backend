"""Private virtual accounts and user-specified practice transactions; no market records."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_paper_accounts"
down_revision = "0006_security_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("initial_cash", sa.Numeric(18, 2), nullable=False),
        sa.Column("orders", postgresql.JSONB(), nullable=False),
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
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 100",
            name=op.f("ck_paper_accounts_name_valid"),
        ),
        sa.CheckConstraint(
            "initial_cash > 0 AND initial_cash <= 1000000000",
            name=op.f("ck_paper_accounts_cash_valid"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_paper_accounts_version_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.users.id"],
            ondelete="CASCADE",
            name=op.f("fk_paper_accounts_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_accounts")),
        schema="app",
    )
    op.create_index(
        "uq_paper_accounts_user_name",
        "paper_accounts",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        schema="app",
    )


def downgrade():
    op.drop_table("paper_accounts", schema="app")
