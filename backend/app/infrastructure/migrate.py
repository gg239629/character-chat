"""Alembic migration entry point; connection secrets never enter ini files."""
from pathlib import Path
from alembic import command
from alembic.config import Config


def upgrade(engine):
    # Register all mapped models before adopting legacy schemas.
    from backend.app.accounts import accounts
    from backend.app.characters import model_defaults
    from backend.app.conversations import chat_history,chat_inbox,peer_chat,push_notifications
    from backend.app.infrastructure import maintenance,model_usage,task_health
    from backend.app.runtime import role_actions
    from backend.app.memory import retrieval
    from backend.app.mcp import access
    from backend.app.world import state_events
    from backend.app.characters import image_generation
    root=Path(__file__).resolve().parents[3]
    cfg=Config(str(root/'alembic.ini'));cfg.set_main_option('script_location',str(root/'migrations'))
    with engine.begin() as connection:
        if engine.dialect.name=='postgresql':
            from sqlalchemy import text
            connection.execute(text('SELECT pg_advisory_xact_lock(710823442)'))
        cfg.attributes['connection']=connection
        command.upgrade(cfg,'head')


if __name__=='__main__':
    from backend.app.infrastructure import storage
    storage.get_store()
    print('Database schema is current; conversation data was not reset.')
