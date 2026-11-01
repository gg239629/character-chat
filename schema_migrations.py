"""Versioned additive migrations. Existing data is never rebuilt or reset."""
from sqlalchemy import text, inspect


def upgrade(engine):
    with engine.begin() as db:
        if engine.dialect.name=='postgresql':db.execute(text('SELECT pg_advisory_xact_lock(710823441)'))
        db.execute(text('CREATE TABLE IF NOT EXISTS chat_schema_versions (version INTEGER PRIMARY KEY)'))
        existing={r[0] for r in db.execute(text('SELECT version FROM chat_schema_versions'))}
        tables=set(inspect(db).get_table_names())
        indexes=[('chat_messages','ix_message_channel_clock','conversation_id, created_at, id'),
            ('chat_inbox_turns','ix_inbox_pending_clock','status, created_at'),
            ('chat_role_actions','ix_action_owner_world','user_id, world_id, created_at'),
            ('chat_peer_jobs','ix_peer_due_status','status, due_at')]
        if 1 not in existing:
            for table,name,columns in indexes:
                if table in tables:db.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})'))
            db.execute(text('INSERT INTO chat_schema_versions (version) VALUES (1)'))
        if 2 not in existing and 'chat_inbox_turns' in tables:
            columns={c['name'] for c in inspect(db).get_columns('chat_inbox_turns')}
            if 'token' not in columns:db.execute(text("ALTER TABLE chat_inbox_turns ADD COLUMN token VARCHAR(32) NOT NULL DEFAULT ''"))
            if 'next_attempt_at' not in columns:db.execute(text('ALTER TABLE chat_inbox_turns ADD COLUMN next_attempt_at FLOAT NOT NULL DEFAULT 0'))
            db.execute(text('CREATE INDEX IF NOT EXISTS ix_inbox_due_status ON chat_inbox_turns (status, next_attempt_at, created_at)'))
            db.execute(text('INSERT INTO chat_schema_versions (version) VALUES (2)'))
        if 3 not in existing and 'chat_task_failures' in tables:
            columns={c['name'] for c in inspect(db).get_columns('chat_task_failures')}
            for name in ('attempts','elapsed_ms'):
                if name not in columns:db.execute(text(f'ALTER TABLE chat_task_failures ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0'))
            db.execute(text('INSERT INTO chat_schema_versions (version) VALUES (3)'))
