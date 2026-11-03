"""Encrypted browser subscriptions and a retryable, privacy-preserving Web Push outbox."""
import base64
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from sqlalchemy import String, Text, Float, select, update, delete, func
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from backend.app.conversations import chat_history
from backend.app.infrastructure import storage
from backend.app.infrastructure import secrets_store


class Subscription(storage.Base):
    __tablename__='chat_push_subscriptions'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id: Mapped[str]=mapped_column(String(128),index=True)
    encrypted: Mapped[str]=mapped_column(Text)


class PushJob(storage.Base):
    __tablename__='chat_push_jobs'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id: Mapped[str]=mapped_column(String(128))
    world_id: Mapped[str]=mapped_column(String(64))
    channel: Mapped[str]=mapped_column(String(8))
    message_id: Mapped[str]=mapped_column(String(64))
    status: Mapped[str]=mapped_column(String(16),default='pending')
    due_at: Mapped[float]=mapped_column(Float)
    attempts: Mapped[int]=mapped_column(default=0)


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def keys():
    key=os.getenv('VAPID_PRIVATE_KEY','').strip()
    if not key:
        if os.getenv('RENDER'):
            return None
        path=Path(__file__).resolve().parents[3]/'data'/'vapid.key'
        path.parent.mkdir(exist_ok=True)
        try:
            with path.open('x') as file:
                private=ec.generate_private_key(ec.SECP256R1())
                file.write(b64(private.private_numbers().private_value.to_bytes(32,'big')))
        except FileExistsError:
            pass
        key=path.read_text().strip()
    from py_vapid import Vapid
    vapid=Vapid.from_string(key)
    public=b64(vapid.public_key.public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint))
    return key,public


def validate(info):
    if not isinstance(info,dict):
        raise ValueError('推送订阅格式无效')
    endpoint=info.get('endpoint','')
    if not isinstance(endpoint,str) or len(endpoint)>3000:
        raise ValueError('推送地址无效')
    parsed=urlparse(endpoint)
    host=(parsed.hostname or '').lower()
    allowed=('fcm.googleapis.com','updates.push.services.mozilla.com','web.push.apple.com')
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443) or len(endpoint)>3000:
        raise ValueError('推送地址无效')
    if host not in allowed and not host.endswith(('.push.services.mozilla.com','.notify.windows.com','.push.apple.com')):
        raise ValueError('暂不支持此浏览器的推送服务')
    raw=info.get('keys',{})
    try:
        public=base64.urlsafe_b64decode(raw['p256dh']+'='*(-len(raw['p256dh'])%4))
        auth=base64.urlsafe_b64decode(raw['auth']+'='*(-len(raw['auth'])%4))
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),public)
        if len(auth)!=16:
            raise ValueError()
    except Exception:
        raise ValueError('推送密钥无效')
    return {'endpoint':endpoint,'keys':{'p256dh':raw['p256dh'],'auth':raw['auth']}}


def subscribe(user,info):
    info=validate(info)
    identity=hashlib.sha256(info['endpoint'].encode()).hexdigest()
    encrypted=secrets_store.cipher().encrypt(json.dumps(info).encode()).decode()
    with Session(storage.get_store().engine) as db:
        row=db.get(Subscription,identity)
        if row:
            if row.user_id!=user:
                raise PermissionError('Subscription unavailable')
            row.encrypted=encrypted
        else:
            if db.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id==user))>=20:
                raise ValueError('订阅设备过多，请先关闭旧设备通知')
            db.add(Subscription(id=identity,user_id=user,encrypted=encrypted))
        db.commit()
    return identity


def unsubscribe(user,endpoint):
    identity=hashlib.sha256(endpoint.encode()).hexdigest()
    with Session(storage.get_store().engine) as db:
        db.execute(delete(Subscription).where(Subscription.id==identity,Subscription.user_id==user));db.commit()


def enqueue(user,world,channel,mid):
    identity=hashlib.sha256(f'{user}:{mid}'.encode()).hexdigest()
    with Session(storage.get_store().engine) as db:
        if not db.scalar(select(Subscription.id).where(Subscription.user_id==user).limit(1)):
            return
        try:
            # Allow visible clients to report read before notifying.
            db.add(PushJob(id=identity,user_id=user,world_id=world,channel=channel,message_id=mid,status='pending',due_at=time.time()+4,attempts=0))
            db.commit()
        except IntegrityError:
            db.rollback()


def flush():
    now=time.time()
    with Session(storage.get_store().engine) as db:
        db.execute(update(PushJob).where(PushJob.status=='pending',PushJob.attempts>=3,PushJob.due_at<=now).values(status='failed'))
        db.commit()
        ids=list(db.scalars(select(PushJob.id).where(PushJob.status=='pending',PushJob.due_at<=now,PushJob.attempts<3).limit(20)))
    if not ids:
        return
    pair=keys()
    if not pair:
        return
    from pywebpush import webpush, WebPushException
    from urllib.parse import urlencode
    for identity in ids:
        with Session(storage.get_store().engine) as db:
            result=db.execute(update(PushJob).where(PushJob.id==identity,PushJob.status=='pending',PushJob.due_at<=now)
                .values(due_at=now+180,attempts=PushJob.attempts+1))
            db.commit()
            if not result.rowcount:
                continue
            job=db.get(PushJob,identity)
            message=db.get(chat_history.Message,job.message_id)
            if not message:
                from backend.app.characters import actors; from backend.app.memory import memory_autosave
                from types import SimpleNamespace
                cid=job.world_id if job.channel=='group' else actors.role_id(job.world_id,job.channel)
                buffered=memory_autosave.message(job.user_id,cid,job.message_id)
                if buffered:message=SimpleNamespace(**buffered,conversation_id=cid)
            if not message:
                job.status='skipped';db.commit();continue
            conversation=db.get(storage.Conversation,message.conversation_id)
            cursor=db.get(chat_history.ReadCursor,message.conversation_id)
            cleared=conversation.data.get('display_cleared_at',0) if conversation else now
            if message.created_at<=cleared or (cursor and cursor.message_time>=message.created_at):
                job.status='read';db.commit();continue
            subs=list(db.scalars(select(Subscription).where(Subscription.user_id==job.user_id)))
            payload=json.dumps({'title':'新消息','body':'你收到了一条新消息','tag':job.world_id+':'+job.channel,
                'url':'/chat?'+urlencode({'conversation':job.world_id,'channel':job.channel}),'message_id':job.message_id})
            failed=False
            for sub in subs:
                try:
                    info=json.loads(secrets_store.cipher().decrypt(sub.encrypted.encode()))
                    validate(info)
                    webpush(subscription_info=info,data=payload,vapid_private_key=pair[0],
                        vapid_claims={'sub':os.getenv('VAPID_SUBJECT','mailto:admin@example.com')},timeout=8,ttl=3600)
                except WebPushException as exc:
                    status=getattr(exc.response,'status_code',None)
                    if status in (404,410):
                        db.delete(sub)
                    else:
                        failed=True
                except Exception:
                    failed=True
            job.status='pending' if failed and job.attempts<3 else 'failed' if failed else 'sent'
            job.due_at=now+60
            db.commit()
