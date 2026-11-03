"""Per-user encrypted model defaults inherited by all stories and channels."""
from sqlalchemy import String,JSON
from sqlalchemy.orm import Mapped,mapped_column,Session
from backend.app.infrastructure import storage
from backend.app.infrastructure import secrets_store

class Defaults(storage.Base):
    __tablename__='chat_model_defaults'
    user_id:Mapped[str]=mapped_column(String(128),primary_key=True)
    settings:Mapped[dict]=mapped_column(JSON)

def read(user):
    with Session(storage.get_store().engine) as db:
        row=db.get(Defaults,user)
        return secrets_store.decode_settings(row.settings) if row else {}

def save(user,settings):
    encoded=secrets_store.encode_settings(settings)
    with Session(storage.get_store().engine) as db:
        row=db.get(Defaults,user)
        if row:row.settings=encoded
        else:db.add(Defaults(user_id=user,settings=encoded))
        db.commit()
