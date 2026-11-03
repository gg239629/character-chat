"""Separate per-client token admission and accounting; preserve existing keys."""
from alembic import op
from backend.app.mcp.access import TokenBudget,TokenHold,Credential
from sqlalchemy import select
revision='0002_mcp_token_budget'
down_revision='0001_adopt_existing'
branch_labels=None
depends_on=None

def upgrade():
    TokenBudget.__table__.create(op.get_bind(),checkfirst=True)
    TokenHold.__table__.create(op.get_bind(),checkfirst=True)
    connection=op.get_bind()
    existing=set(connection.scalars(select(TokenBudget.credential_id)))
    rows=[dict(credential_id=row.id,user_id=row.user_id,daily_tokens=0,day=0,used=0,reserved=0,estimated_calls=0)
        for row in connection.execute(select(Credential.id,Credential.user_id)) if row.id not in existing]
    if rows:connection.execute(TokenBudget.__table__.insert(),rows)
def downgrade():raise RuntimeError('Credential budget history requires an explicit data migration')
