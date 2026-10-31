"""Copy local SQLite to a new PostgreSQL database without overwriting data."""
import getpass
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import select
from storage import Store, User, Conversation


def copy_rows(source_path, destination):
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    try:
        users = source.execute("SELECT id FROM chat_users").fetchall()
        rows = source.execute("SELECT id, user_id, data, version FROM chat_conversations").fetchall()
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
