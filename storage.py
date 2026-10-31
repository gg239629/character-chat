"""SQLAlchemy storage, scoped by trusted user identity and conversation ownership."""
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4
import json
import os
from sqlalchemy import create_engine, String, JSON, ForeignKey, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parent
scope = ContextVar("memory_scope", default=("owner", "default"))

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "chat_users"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)

class Conversation(Base):
    __tablename__ = "chat_conversations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("chat_users.id"), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(default=0)

class Store:
    def __init__(self, url):
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.engine = create_engine(url, pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
        Base.metadata.create_all(self.engine)

    def ensure(self, user, conversation, seed=None):
        with Session(self.engine) as db:
            existing = db.get(Conversation, conversation)
            if existing:
                if existing.user_id != user:
                    raise PermissionError("Conversation does not belong to current user")
                return
            try:
                if not db.get(User, user):
                    db.add(User(id=user)); db.flush()
                db.add(Conversation(id=conversation, user_id=user, data=seed or {}))
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.get(Conversation, conversation)
                if not existing or existing.user_id != user:
                    raise PermissionError("Conversation unavailable")

    def read(self, user, conversation):
        with Session(self.engine) as db:
            row = db.get(Conversation, conversation)
            if not row or row.user_id != user:
                raise PermissionError("Conversation unavailable")
            return row.data

    def mutate(self, user, conversation, change):
        # Optimistic revision checks prevent concurrent scene/archive saves
        # from replacing each other's fields, including on SQLite.
        for _ in range(10):
            with Session(self.engine) as db:
                row = db.get(Conversation, conversation)
                if not row or row.user_id != user:
                    raise PermissionError("Conversation unavailable")
                data = json.loads(json.dumps(row.data))
                result = change(data)
                from sqlalchemy import update
                updated = db.execute(update(Conversation).where(
                    Conversation.id == conversation, Conversation.user_id == user,
                    Conversation.version == row.version).values(data=data, version=row.version+1))
                if updated.rowcount:
                    db.commit(); return result
                db.rollback()
        raise RuntimeError("Concurrent memory updates; retry request")

    def conversations(self, user):
        with Session(self.engine) as db:
            return [{"id": row.id} for row in db.scalars(select(Conversation).where(Conversation.user_id == user))]

_STORE = None

def get_store():
    global _STORE
    if _STORE is None:
        url = os.getenv("DATABASE_URL")
        if not url:
            if os.getenv("RENDER"):
                raise RuntimeError("Set DATABASE_URL to a persistent PostgreSQL database on Render")
            folder = ROOT / "data"
            folder.mkdir(exist_ok=True)
            url = "sqlite:///" + (folder / "chat.db").as_posix()
        _STORE = Store(url)
    return _STORE

def initialize():
    seed = {}
    legacy = ROOT / "memory" / "记忆包.json"
    if legacy.exists():
        pack = json.loads(legacy.read_text(encoding="utf-8-sig"))
        seed = {"scene_state": pack.get("scene_state", {}), "recent_log": pack.get("recent_log", [])}
    for name in ("archive.json", "summary.json"):
        path = ROOT / "memory" / "long_term" / name
        if path.exists():
            seed[name] = json.loads(path.read_text(encoding="utf-8"))
    if "archive.json" not in seed:
        seed["archive.json"] = [{"id": uuid4().hex, **item} for item in seed.get("recent_log", [])]
    # Old private memory is imported once, into the owner's default conversation only.
    get_store().ensure("owner", "default", seed)

def read_document(name, default):
    return get_store().read(*scope.get()).get(name, default)

def write_document(name, value):
    get_store().mutate(*scope.get(), lambda data: data.__setitem__(name, value))

def memory_pack():
    data = get_store().read(*scope.get())
    return {"scene_state": data.get("scene_state", {}), "recent_log": data.get("recent_log", [])}

def append_log(items):
    def change(data):
        log = data.get("recent_log", []) + items
        data["recent_log"] = log[-200:]
        # Preserve full chronological originals separately from the short recent window.
        records = data.get("archive.json", [])
        records += [{"id": uuid4().hex, **item} for item in items]
        data["archive.json"] = records
        return len(data["recent_log"])
    return get_store().mutate(*scope.get(), change)
