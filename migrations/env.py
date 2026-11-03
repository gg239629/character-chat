from alembic import context
from backend.app.infrastructure import storage

connection=context.config.attributes.get('connection')
if connection is None:
    raise RuntimeError('Use python -m backend.app.infrastructure.migrate, which reads the private database configuration')
context.configure(connection=connection,target_metadata=storage.Base.metadata,compare_type=True)
with context.begin_transaction():context.run_migrations()
