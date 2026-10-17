"""Create application and market namespaces; no business tables yet."""
from alembic import op

revision = "0001_create_schemas"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA app")
    op.execute("CREATE SCHEMA market")


def downgrade():
    # Intentionally no CASCADE: never erase tables introduced outside this revision.
    op.execute("DROP SCHEMA market")
    op.execute("DROP SCHEMA app")
