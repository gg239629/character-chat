from alembic import op
import sqlalchemy as sa
revision='0003_usage_run'
down_revision='0002_mcp_token_budget'
branch_labels=None
depends_on=None

def upgrade():
    if 'run_id' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('chat_model_usage')}:
        op.add_column('chat_model_usage',sa.Column('run_id',sa.String(64),nullable=True))
def downgrade():raise RuntimeError('Usage history migration requires explicit review')
