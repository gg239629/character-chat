"""Adopt existing installations without rebuilding or copying their data."""
from alembic import op
from backend.app.infrastructure import storage, schema_migrations

revision='0001_adopt_existing'
down_revision=None
branch_labels=None
depends_on=None


def upgrade():
    connection=op.get_bind()
    storage.Base.metadata.create_all(connection)
    schema_migrations.upgrade_connection(connection)


def downgrade():
    raise RuntimeError('Data-preserving baseline cannot be downgraded automatically')
