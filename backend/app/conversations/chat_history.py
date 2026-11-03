"""Database-backed messages, idempotent turn IDs and per-window generation leases."""
import hashlib
import json
import time
from uuid import uuid4
from datetime import datetime, timezone
from sqlalchemy import String, Float, Text, select, update, delete, func
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from backend.app.infrastructure import storage


class Message(storage.Base):
    __tablename__ = 'chat_messages'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    speaker: Mapped[str] = mapped_column(String(16), default='')
    created_at: Mapped[float] = mapped_column(Float)


class Lease(storage.Base):
    __tablename__ = 'chat_generation_leases'
    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token: Mapped[str] = mapped_column(String(64))
    expires: Mapped[float] = mapped_column(Float)


class ReadCursor(storage.Base):
    __tablename__ = 'chat_read_cursors'
    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_time: Mapped[float] = mapped_column(Float, default=0)


def summaries():
    """Batch latest-message previews and assistant unread counts for this owner."""
    from sqlalchemy import func
    user=storage.scope.get()[0]
    with Session(storage.get_store().engine) as db:
        ids=list(db.scalars(select(storage.Conversation.id).where(storage.Conversation.user_id==user)))
        cutoff=func.coalesce(storage.Conversation.data['display_cleared_at'].as_float(),0)
        ranked=select(Message.conversation_id,Message.content,Message.speaker,Message.created_at,
            func.row_number().over(partition_by=Message.conversation_id,order_by=(Message.created_at.desc(),Message.id.desc())).label('position')).join(storage.Conversation,storage.Conversation.id==Message.conversation_id).where(Message.conversation_id.in_(ids),Message.created_at>cutoff).subquery()
        result={row.conversation_id:{'preview':row.content[:180],'speaker':row.speaker,'updated_at':row.created_at,'unread':0}
            for row in db.execute(select(ranked).where(ranked.c.position==1))}
        counts=db.execute(select(Message.conversation_id,func.count(),func.max(Message.created_at)).join(storage.Conversation,storage.Conversation.id==Message.conversation_id).outerjoin(ReadCursor,ReadCursor.conversation_id==Message.conversation_id)
            .where(Message.conversation_id.in_(ids),Message.created_at>cutoff,Message.role=='assistant',Message.created_at>func.coalesce(ReadCursor.message_time,0)).group_by(Message.conversation_id))
        for identity,count,incoming_at in counts:
            result[identity]['unread']=count
            result[identity]['incoming_at']=incoming_at
        from backend.app.memory import memory_autosave
        for owner,conversation,pending in memory_autosave.all_pending():
            if owner!=user or conversation not in ids:continue
            data=storage.get_store().read(user,conversation)
            cursor=db.get(ReadCursor,conversation)
            visible=[item for item in pending if item['created_at']>data.get('display_cleared_at',0)]
            if not visible:continue
            newest=max(visible,key=lambda item:item['created_at'])
            current=result.setdefault(conversation,{'unread':0,'updated_at':0})
            if newest['created_at']>=current['updated_at']:
                current.update(preview=newest['content'][:180],speaker=newest['speaker'],updated_at=newest['created_at'])
            unread=[item for item in visible if item['role']=='assistant' and item['created_at']>(cursor.message_time if cursor else 0)]
            current['unread']+=len(unread)
            if unread:current['incoming_at']=max(current.get('incoming_at',0),max(item['created_at'] for item in unread))
        return result


def mark_read(message_id):
    user,conversation=storage.scope.get()
    storage.get_store().read(user,conversation)
    for _ in range(3):
        with Session(storage.get_store().engine) as db:
            message=db.get(Message,message_id)
            if not message:
                from backend.app.memory import memory_autosave
                from types import SimpleNamespace
                buffered=memory_autosave.message(user,conversation,message_id)
                message=SimpleNamespace(**buffered,conversation_id=conversation) if buffered else None
            if not message or message.conversation_id!=conversation:
                raise ValueError('消息不属于当前会话')
            row=db.get(ReadCursor,conversation)
            if row:
                db.execute(update(ReadCursor).where(ReadCursor.conversation_id==conversation,ReadCursor.message_time<message.created_at).values(message_time=message.created_at))
            else:
                db.add(ReadCursor(conversation_id=conversation,message_time=message.created_at))
            try:
                db.commit();return
            except IntegrityError:
                db.rollback()
    raise RuntimeError('已读状态保存冲突，请重试')


def identity(conversation, turn, sequence):
    return hashlib.sha256(f'{conversation}:{turn}:{sequence}'.encode()).hexdigest()


def append(turn, sequence, role, content, speaker='', mirror=True, metadata=None):
    """Accept a message into this server's minute buffer; checkpoint it asynchronously."""
    from backend.app.memory import memory_autosave
    return memory_autosave.append(turn,sequence,role,content,speaker,mirror,metadata)


def bootstrap():
    """Import the existing recent log once without duplicating memory records."""
    if storage.read_document('messages_initialized', False):
        return
    user, conversation = storage.scope.get()
    for _ in range(10):
        with Session(storage.get_store().engine) as db:
            row = db.get(storage.Conversation,conversation)
            if not row or row.user_id != user:
                raise PermissionError('Conversation unavailable')
            if row.data.get('messages_initialized'):
                return
            data=json.loads(json.dumps(row.data))
            result=db.execute(update(storage.Conversation).where(storage.Conversation.id==conversation,
                storage.Conversation.version==row.version).values(data={**data,'messages_initialized':True},version=row.version+1))
            if not result.rowcount:
                db.rollback()
                continue
            stamp=time.time()
            for i,item in enumerate(data.get('recent_log',[])):
                who=item.get('who','')
                role='user' if who in storage.USER_NAMES else 'assistant'
                text=item.get('text','')
                if role=='assistant' and not text.startswith(who+'：'):
                    text=who+'：'+text
                db.add(Message(id=identity(conversation,'legacy',i),conversation_id=conversation,
                    role=role,content=text,speaker='' if role=='user' else who,created_at=stamp+i/1000000))
            db.commit()
            cached=storage.request_snapshot.get()
            if cached and cached['scope']==(user,conversation):
                cached['data']={**data,'messages_initialized':True}
            return
    raise RuntimeError('Concurrent history migration; retry')


def clear_display():
    """Atomically clear the visible window and unread count, preserving all memories."""
    user,conversation=storage.scope.get()
    for _ in range(10):
        with Session(storage.get_store().engine) as db:
            row=db.get(storage.Conversation,conversation)
            if not row or row.user_id!=user:raise PermissionError('Conversation unavailable')
            latest=db.scalar(select(func.max(Message.created_at)).where(Message.conversation_id==conversation)) or 0
            data=json.loads(json.dumps(row.data))
            from backend.app.memory import memory_autosave
            buffered=max([item['created_at'] for item in memory_autosave.pending(user,conversation)]+[0])
            cutoff=max(time.time(),latest,buffered,data.get('display_cleared_at',0))
            data['display_cleared_at']=cutoff
            result=db.execute(update(storage.Conversation).where(storage.Conversation.id==conversation,
                storage.Conversation.version==row.version).values(data=data,version=row.version+1))
            if not result.rowcount:db.rollback();continue
            cursor=db.get(ReadCursor,conversation)
            if cursor:cursor.message_time=max(cursor.message_time,cutoff)
            else:db.add(ReadCursor(conversation_id=conversation,message_time=cutoff))
            try:db.commit()
            except IntegrityError:db.rollback();continue
            snapshot=storage.request_snapshot.get()
            if snapshot is not None and snapshot['scope']==(user,conversation):snapshot['data']=data
            return cutoff
    raise RuntimeError('清除记录保存冲突，请重试')


def visible_messages(limit=200):
    return messages(limit,visible=True)


def poll_visible(after=''):
    """Unchanged polls query only the indexed head, never 200 message bodies."""
    if not after:return visible_messages(),False
    from backend.app.memory import memory_autosave
    user,cid=storage.scope.get()
    data=storage.get_store().read(user,cid)
    cutoff=data.get('display_cleared_at',0)
    with Session(storage.get_store().engine) as db:
        head=db.execute(select(Message.id,Message.created_at).where(Message.conversation_id==cid,Message.created_at>cutoff)
            .order_by(Message.created_at.desc(),Message.id.desc()).limit(1)).first()
    candidates=[(head.created_at,head.id)] if head else []
    candidates.extend((m['created_at'],m['id']) for m in memory_autosave.pending(user,cid) if m['created_at']>cutoff)
    newest=max(candidates,default=(0,''))[1]
    if newest and newest==after:return [],True
    return visible_messages(),False


def updates(after=''):
    if not after:return visible_messages(),False,False
    from backend.app.memory import memory_autosave
    user,cid=storage.scope.get();cutoff=storage.get_store().read(user,cid).get('display_cleared_at',0)
    pending=[m for m in memory_autosave.pending(user,cid) if m['created_at']>cutoff]
    with Session(storage.get_store().engine) as db:
        cursor=db.execute(select(Message.id,Message.created_at).where(Message.conversation_id==cid,Message.id==after,
            Message.created_at>cutoff)).first()
        if cursor is None:cursor=next((m for m in pending if m['id']==after),None)
        if cursor is None:return visible_messages(),False,False
        stamp=cursor['created_at'] if isinstance(cursor,dict) else cursor.created_at
        newer=(Message.created_at>stamp)|((Message.created_at==stamp)&(Message.id>after))
        rows=list(db.scalars(select(Message).where(Message.conversation_id==cid,newer)
            .order_by(Message.created_at,Message.id).limit(201)))
        records=[{'id':r.id,'role':r.role,'content':r.content,'speaker':r.speaker,'created_at':r.created_at} for r in rows]
    records.extend({k:m[k] for k in ('id','role','content','speaker','created_at')} for m in pending if (m['created_at'],m['id'])>(stamp,after))
    records=sorted({m['id']:m for m in records}.values(),key=lambda m:(m['created_at'],m['id']))
    if len(records)>200:return visible_messages(),False,False
    return records,not bool(records),True


def messages(limit=200,visible=False):
    user, conversation = storage.scope.get()
    storage.get_store().read(user, conversation)
    with Session(storage.get_store().engine) as db:
        query=select(Message).where(Message.conversation_id == conversation)
        if visible:
            query=query.where(Message.created_at>storage.read_document('display_cleared_at',0))
        rows = list(db.scalars(query
            .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)))
        saved=[{'id': row.id, 'role': row.role, 'content': row.content, 'speaker': row.speaker, 'created_at': row.created_at} for row in rows]
    from backend.app.memory import memory_autosave
    cutoff=storage.read_document('display_cleared_at',0) if visible else 0
    saved += [{key:item[key] for key in ['id','role','content','speaker','created_at']} for item in memory_autosave.pending(user,conversation) if item['created_at']>cutoff]
    unique={item['id']:item for item in saved}
    return sorted(unique.values(),key=lambda item:(item['created_at'],item['id']))[-limit:]


def claim(turn,accepted=False):
    """One active generation per window, including across server workers."""
    user, conversation = storage.scope.get()
    storage.get_store().read(user, conversation)
    from backend.app.memory import memory_autosave
    if not accepted and memory_autosave.message(user,conversation,identity(conversation,turn,0)):return False
    with Session(storage.get_store().engine) as db:
        if not accepted and db.get(Message, identity(conversation, turn, 0)):
            return False
        try:
            db.add(Lease(conversation_id=conversation, token=turn, expires=time.time()+600))
            db.commit()
        except IntegrityError:
            db.rollback()
            result = db.execute(update(Lease).where(Lease.conversation_id == conversation,
                Lease.expires < time.time()).values(token=turn, expires=time.time()+600))
            db.commit()
            if not result.rowcount:
                raise RuntimeError('当前会话正在生成，请稍后再发送')
    return True


def release(turn):
    with Session(storage.get_store().engine) as db:
        db.execute(delete(Lease).where(Lease.conversation_id == storage.scope.get()[1], Lease.token == turn))
        db.commit()


def import_browser(items):
    """One-time recovery of unsaved browser messages, deduplicated by content."""
    # Never recover a legacy browser cache into a cleared window.
    if storage.read_document('display_cleared_at',0):return 0
    existing = messages(10000)
    # Existing may already contain this exact browser suffix (or older subset).
    def key(m):
        return m['role'], m['content'].strip()
    live = [key(m) for m in items]
    saved = [key(m) for m in existing]
    if not live or any(saved[i:i+len(live)] == live for i in range(len(saved)-len(live)+1)):
        return 0
    overlap = 0
    for size in range(min(len(live),len(saved)),0,-1):
        if saved[-size:] == live[:size]:
            overlap = size
            break
    fresh = items[overlap:]
    token = 'import-' + hashlib.sha256(json.dumps(items,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:48]
    count = 0
    for i, item in enumerate(fresh):
        speaker = next((n for n in ('小婉','小研','小柳') if item['content'].startswith(n+'：')), '')
        count += append(token, i, item['role'], item['content'], speaker)
    return count
