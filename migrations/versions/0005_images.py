"""Persist image configuration, generation tasks and binary assets."""
from alembic import op
from backend.app.characters.image_generation import Settings,Job
revision='0005_images'
down_revision='0004_world_events'
branch_labels=None
depends_on=None


def upgrade():
    for model in (Settings,Job):model.__table__.create(op.get_bind(),checkfirst=True)


def downgrade():raise RuntimeError('Image history requires an explicit data migration')
