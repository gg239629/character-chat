"""Copy local SQLite to a new PostgreSQL database without overwriting data."""
import getpass
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import select
from storage import Store, User, Conversation
from chat_history import Message
from chat_history import ReadCursor
from role_actions import Action, Checkpoint
from push_notifications import Subscription, PushJob
from maintenance import Job as MaintenanceJob
from model_usage import Usage
from peer_chat import Message as PeerMessage, Archive as PeerArchive, Job as PeerJob
from chat_inbox import Turn as InboxTurn
from accounts import Account, LoginSession
from task_health import Failure


def copy_rows(source_path, destination):
    import storage, memory_autosave
    engine=storage.get_store().engine
    if engine.dialect.name=='sqlite' and Path(engine.url.database).resolve()==source_path.resolve():
        memory_autosave.flush_due(force=True)
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    try:
        users = source.execute("SELECT id FROM chat_users").fetchall()
        rows = source.execute("SELECT id, user_id, data, version FROM chat_conversations").fetchall()
        has_messages = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_messages'").fetchone()
        messages = source.execute("SELECT id, conversation_id, role, content, speaker, created_at FROM chat_messages").fetchall() if has_messages else []
        extras=[]
        from sqlalchemy import JSON
        for model in (ReadCursor,Action,Checkpoint,Subscription,PushJob,MaintenanceJob,Usage,PeerMessage,PeerArchive,PeerJob,InboxTurn,Account,LoginSession,Failure):
            table=model.__table__
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table.name,)).fetchone():
                continue
            columns=list(table.columns)
            existing={r[1] for r in source.execute('PRAGMA table_info("'+table.name+'")')}
            columns=[column for column in columns if column.name in existing]
            records=source.execute('SELECT '+', '.join('"'+column.name+'"' for column in columns)+' FROM "'+table.name+'"').fetchall()
            extras.append((model,[{column.name:json.loads(value) if isinstance(column.type,JSON) and value is not None else value
                for column,value in zip(columns,row)} for row in records]))
    finally:
        source.close()
    with Session(destination.engine) as db:
        if db.scalar(select(Conversation.id).limit(1)) is not None:
            raise RuntimeError("Target already contains conversations; nothing copied. Do not overwrite.")
        for (identity,) in users:
            if not db.get(User, identity):
                db.add(User(id=identity))
        db.flush()
        for identity, owner, data, version in rows:
            db.add(Conversation(id=identity, user_id=owner, data=json.loads(data), version=version))
        db.flush()
        for identity, conversation, role, content, speaker, created in messages:
            db.add(Message(id=identity, conversation_id=conversation, role=role, content=content, speaker=speaker, created_at=created))
        for model,records in extras:
            for record in records:
                db.add(model(**record))
        db.flush()
        copied = db.execute(select(Message.id, Message.conversation_id, Message.role, Message.content, Message.speaker, Message.created_at)).all()
        assert sorted(tuple(row) for row in copied) == sorted(messages)
        for identity, owner, data, version in rows:
            assert db.get(Conversation, identity).data == json.loads(data)
        db.commit()
    for identity, owner, data, version in rows:
        assert destination.read(owner, identity) == json.loads(data)
    return len(rows)


def main():
    source = Path(__file__).resolve().parent / "data" / "chat.db"
    if not source.exists():
        raise RuntimeError("Local data/chat.db is missing")
    backups = source.parent / "backups"
    backups.mkdir(exist_ok=True)
    backup = backups / ("chat-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
    import memory_autosave
    memory_autosave.flush_due(force=True)
    with sqlite3.connect(source) as db, sqlite3.connect(backup) as target:
        db.backup(target)
    print("Local backup created:", backup.name)
    url = getpass.getpass("Paste Neon PostgreSQL connection string (hidden): ").strip()
    if not url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")) or "***" in url:
        raise RuntimeError("Paste the real PostgreSQL URI, without psql or surrounding quotes")
    destination = None
    try:
        destination = Store(url)
        count = copy_rows(backup, destination)
        print(f"SUCCESS: copied and verified {count} conversations. Local database unchanged.")
    finally:
        if destination:
            destination.engine.dispose()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Connection errors can contain credentials: never print the raw exception.
        print("Migration stopped:", type(exc).__name__)
        print("Check connectivity and whether target already has data. Local database is preserved.")
        raise SystemExit(1)
