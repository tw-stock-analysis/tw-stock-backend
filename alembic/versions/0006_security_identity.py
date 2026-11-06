"""Evidence-dated durable security identities; metadata only, no market observations."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_security_identity"
down_revision = "0005_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "security_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("registry_key", sa.String(160), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("instrument_type", sa.String(32), nullable=False),
        sa.Column("issuer_ubn", sa.String(8), nullable=True),
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
            "instrument_type IN ('ordinary_share','etf','other')",
            name=op.f("ck_security_identities_type_allowed"),
        ),
        sa.CheckConstraint(
            "issuer_ubn IS NULL OR issuer_ubn ~ '^[0-9]{8}$'",
            name=op.f("ck_security_identities_issuer_ubn_format"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_identities")),
        sa.UniqueConstraint("registry_key", name=op.f("uq_security_identities_registry_key")),
        schema="app",
    )
    op.create_table(
        "security_listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("start_evidence", postgresql.JSONB(), nullable=False),
        sa.Column("end_evidence", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "market IN ('TWSE','TPEX','EMERGING')", name=op.f("ck_security_listings_market_allowed")
        ),
        sa.CheckConstraint(
            "code ~ '^[0-9A-Z]{4,6}$'", name=op.f("ck_security_listings_code_format")
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name=op.f("ck_security_listings_valid_interval"),
        ),
        sa.CheckConstraint(
            "(valid_to IS NULL) = (end_evidence IS NULL)",
            name=op.f("ck_security_listings_end_evidence_required"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["app.security_identities.id"],
            ondelete="CASCADE",
            name=op.f("fk_security_listings_security_id_security_identities"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_listings")),
        schema="app",
    )
    op.create_index(
        "uq_security_listings_identity_market_code_start",
        "security_listings",
        ["security_id", "market", "code", "valid_from"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_security_listings_market_code_dates",
        "security_listings",
        ["market", "code", "valid_from", "valid_to"],
        schema="app",
    )
    op.create_table(
        "security_identifiers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(200), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("end_evidence", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "(valid_to IS NULL) = (end_evidence IS NULL)",
            name=op.f("ck_security_identifiers_end_evidence_required"),
        ),
        sa.CheckConstraint(
            "kind IN ('isin','name')", name=op.f("ck_security_identifiers_kind_allowed")
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name=op.f("ck_security_identifiers_valid_interval"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["app.security_identities.id"],
            ondelete="CASCADE",
            name=op.f("fk_security_identifiers_security_id_security_identities"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_identifiers")),
        schema="app",
    )
    op.create_index(
        "uq_security_identifiers_identity_kind_value_start",
        "security_identifiers",
        ["security_id", "kind", "value", "valid_from"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_security_identifiers_kind_value",
        "security_identifiers",
        ["kind", "value"],
        schema="app",
    )


def downgrade():
    op.drop_table("security_identifiers", schema="app")
    op.drop_table("security_listings", schema="app")
    op.drop_table("security_identities", schema="app")
