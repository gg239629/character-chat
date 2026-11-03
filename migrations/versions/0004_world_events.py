"""Add persistent world evidence without replacing existing conversations."""
from alembic import op
from backend.app.world.state_events import Event

revision='0004_world_events'
down_revision='0003_usage_run'
branch_labels=None
depends_on=None


def upgrade():
    Event.__table__.create(op.get_bind(),checkfirst=True)


def downgrade():
    raise RuntimeError('World event history requires an explicit data migration')
