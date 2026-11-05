"""Persist asynchronous character voice replies."""
from alembic import op
from backend.app.characters.voice_generation import Job
revision='0006_voice'
down_revision='0005_images'
branch_labels=None
depends_on=None

def upgrade():Job.__table__.create(op.get_bind(),checkfirst=True)
def downgrade():raise RuntimeError('Voice history requires an explicit data migration')
