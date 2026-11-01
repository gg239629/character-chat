"""Optional local accounts with hashed passwords and revocable opaque sessions."""
import hashlib
import hmac
import secrets
import time
import os
from sqlalchemy import String, Float, select, delete
from sqlalchemy.orm import Mapped,mapped_column,Session
import storage


class Account(storage.Base):
    __tablename__='chat_accounts'
    name:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),unique=True)
    password_hash:Mapped[str]=mapped_column(String(256))


class LoginSession(storage.Base):
    __tablename__='chat_login_sessions'
    token_hash:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    expires:Mapped[float]=mapped_column(Float)


def enabled():return os.getenv('ENABLE_USER_ACCOUNTS','false').lower() in ('1','true','yes')


def password_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex()


def register(name,password):
    import re
    name=name.strip().lower()
    if not re.fullmatch(r'[a-z0-9_\-]{3,40}',name) or not 10<=len(password)<=128:
        raise ValueError('用户名需为3至40位字母、数字、下划线或短横线，密码需为10至128位')
    salt=secrets.token_hex(16);user='u_'+secrets.token_hex(16)
    from sqlalchemy.exc import IntegrityError
    with Session(storage.get_store().engine) as db:
        db.add(Account(name=name,user_id=user,password_hash=salt+':'+password_hash(password,salt)))
        db.add(storage.User(id=user))
        db.add(storage.Conversation(id=root_id(user),user_id=user,data={'title':'大家一起','recent_log':[],
            'story_clock':{'day':1,'minute':480},'scene_state':{'时间':'第一天早晨08:00',
                **{name:'卧室·刚起床' for name in ('小婉','小研','小柳')}}},version=0))
        try:db.flush();db.commit()
        except IntegrityError:raise ValueError('用户名已被使用')
    return user


def root_id(user):return hashlib.sha256(('account-story:'+user).encode()).hexdigest()[:48]


def login(name,password):
    with Session(storage.get_store().engine) as db:
        row=db.get(Account,name.strip().lower())
        salt,digest=row.password_hash.split(':') if row else ('0'*32,'0'*64)
        valid=hmac.compare_digest(password_hash(password,salt),digest)
        if not row or not valid:raise ValueError('用户名或密码错误')
        token=secrets.token_urlsafe(32)
        db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=row.user_id,expires=time.time()+7*86400))
        db.execute(delete(LoginSession).where(LoginSession.expires<time.time()));db.commit()
        return token


def identify(token):
    if not token:return None
    with Session(storage.get_store().engine) as db:
        row=db.get(LoginSession,hashlib.sha256(token.encode()).hexdigest())
        return row.user_id if row and row.expires>time.time() else None


def logout(token):
    with Session(storage.get_store().engine) as db:
        db.execute(delete(LoginSession).where(LoginSession.token_hash==hashlib.sha256(token.encode()).hexdigest()));db.commit()
