"""Durable user inbox: acceptance is independent from character generation."""
import asyncio
from contextvars import ContextVar
import hashlib
import json
import time
from uuid import uuid4
from sqlalchemy import String, Float, select, update, or_
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from backend.app.infrastructure import storage

processing=ContextVar('inbox_processing',default=False)


class Turn(storage.Base):
    __tablename__='chat_inbox_turns'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    world_id:Mapped[str]=mapped_column(String(64))
    channel:Mapped[str]=mapped_column(String(8))
    request_id:Mapped[str]=mapped_column(String(128))
    content:Mapped[str]=mapped_column(String(100000))
    status:Mapped[str]=mapped_column(String(16),index=True)
    created_at:Mapped[float]=mapped_column(Float)
    lease_until:Mapped[float]=mapped_column(Float,default=0)
    attempts:Mapped[int]=mapped_column(default=0)
    token:Mapped[str]=mapped_column(String(32),default='')
    next_attempt_at:Mapped[float]=mapped_column(Float,default=0)


def accept(body):
    from backend.app.core import context as execution_context, services
    from backend.app.conversations import chat_history
    from backend.app.memory import memory_autosave
    user,cid=storage.scope.get();world=execution_context.chat_world.get()
    text=next((m.get('content','').strip() for m in reversed(body.messages) if m.get('role')=='user'),'')
    if not text or len(text)>100000:raise ValueError('请输入有效消息（不超过十万字）')
    identity=hashlib.sha256(f'{user}:{cid}:{body.request_id}'.encode()).hexdigest()
    with Session(storage.get_store().engine) as db:
        existing=db.get(Turn,identity)
        if existing:
            if existing.content!=text:raise ValueError('重复请求内容不一致')
            return {'id':identity,'status':existing.status}
        chat_history.bootstrap()
        memory_autosave.flush(user,cid,force=True)
        from backend.app.conversations import message_service
        message,_=message_service.write(db,user,cid,body.request_id,0,'user',text)
        accepted_at=message.created_at
        db.add(Turn(id=identity,user_id=user,world_id=world['id'],channel=world['channel'],
            request_id=body.request_id,content=text,status='pending',created_at=accepted_at,lease_until=0,attempts=0))
        try:db.commit()
        except IntegrityError:
            db.rollback()
            previous=db.get(Turn,identity)
            if not previous or previous.content!=text:raise
    return {'id':identity,'status':'pending'}


def state(identity):
    from backend.app.core import context as execution_context, services
    user=storage.scope.get()[0];world=execution_context.chat_world.get()
    with Session(storage.get_store().engine) as db:
        row=db.get(Turn,identity)
        if not row or (row.user_id,row.world_id,row.channel)!=(user,world['id'],world['channel']):
            raise PermissionError('Message unavailable')
        return {'id':identity,'status':row.status}


def retry(identity):
    from backend.app.conversations import chat_history
    state(identity)  # Ownership and original channel check.
    with Session(storage.get_store().engine) as db:
        row=db.get(Turn,identity)
        if row.status!='failed':raise ValueError('仅失败的回复任务可以重试')
        if chat_history.messages() and any(m['role']=='assistant' and m['created_at']>row.created_at for m in chat_history.messages()):
            raise ValueError('此后已有角色回复，请直接发送新消息继续，避免重复回答')
        row.status='pending';row.attempts=0;row.lease_until=0;row.next_attempt_at=0;row.token='';db.commit()
    return {'id':identity,'status':'pending'}


async def process(identity):
    from backend.app.runtime.graph import execute
    await execute(identity,_process)


async def _process(identity):
    from backend.app.core import context as execution_context, services
    from backend.app.runtime import role_actions
    from backend.app.conversations import chat_history
    now=time.time()
    owner_token=uuid4().hex
    with Session(storage.get_store().engine) as db:
        row=db.get(Turn,identity)
        if not row:return
        updated=db.execute(update(Turn).where(Turn.id==identity,Turn.attempts<3,Turn.next_attempt_at<=now,
            or_(Turn.status=='pending',(Turn.status=='running')&(Turn.lease_until<now)))
            .values(status='running',lease_until=now+660,token=owner_token,attempts=Turn.attempts+1))
        if not updated.rowcount:db.rollback();return
        user,world,channel,request,content,created=row.user_id,row.world_id,row.channel,row.request_id,row.content,row.created_at
        attempt=row.attempts
        db.commit()
    run_token=execution_context.model_run.set(identity)
    status='done';busy=False
    async def heartbeat():
        from backend.app.characters import actors
        cid=world if channel=='group' else actors.role_id(world,channel)
        def renew():
            with Session(storage.get_store().engine) as db:
                result=db.execute(update(Turn).where(Turn.id==identity,Turn.token==owner_token,Turn.status=='running')
                    .values(lease_until=time.time()+660))
                if result.rowcount:
                    db.execute(update(chat_history.Lease).where(chat_history.Lease.conversation_id==cid,
                        chat_history.Lease.token==request).values(expires=time.time()+600))
                db.commit()
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(renew)
    pulse=asyncio.create_task(heartbeat())
    try:
        with role_actions.window(user,world,channel):
            # A newer accepted message supersedes older pending turns. The latest
            # turn sees all originals; it does not need a separate answer per keystroke.
            with Session(storage.get_store().engine) as db:
                newer=db.scalar(select(Turn.id).where(Turn.user_id==user,Turn.world_id==world,Turn.channel==channel,
                    Turn.created_at>created,Turn.status!='failed').limit(1))
            if newer:status='superseded';return
            token=processing.set(True)
            try:
                from backend.app.core.schemas import ChatIn
                response=await services.generation().chat_stream(ChatIn(request_id=request,messages=[{'role':'user','content':content}]))
                if getattr(response,'status_code',200)!=200:
                    status='pending' if response.status_code==409 else 'failed'
                    busy=response.status_code==409
                else:
                    async for chunk in response.body_iterator:
                        raw=chunk.decode() if isinstance(chunk,bytes) else chunk
                        for line in raw.splitlines():
                            if line.startswith('data:') and json.loads(line[5:]).get('error'):
                                status='failed'
                                from backend.app.infrastructure import task_health
                                await asyncio.to_thread(task_health.record,user,world,identity,'model_reply',RuntimeError(),attempt,int((time.time()-now)*1000))
            finally:processing.reset(token)
    except Exception as error:
        from backend.app.infrastructure import task_health
        await asyncio.to_thread(task_health.record,user,world,identity,'reply',error,attempt,int((time.time()-now)*1000))
        status='pending'
    finally:
        pulse.cancel()
        await asyncio.gather(pulse,return_exceptions=True)
        with Session(storage.get_store().engine) as db:
            item=db.get(Turn,identity)
            if item and item.token==owner_token:
                if busy:item.attempts=max(0,item.attempts-1)
                item.status='failed' if status=='pending' and item.attempts>=3 else status
                item.next_attempt_at=time.time()+(1 if busy else min(60,2**item.attempts)) if item.status=='pending' else 0
                item.lease_until=0;db.commit()
        execution_context.model_run.reset(run_token)


def run():
    now=time.time()
    with Session(storage.get_store().engine) as db:
        db.execute(update(Turn).where(Turn.status=='running',Turn.lease_until<now,Turn.attempts>=3).values(status='failed'))
        db.commit()
        rows=list(db.scalars(select(Turn).where(Turn.attempts<3,Turn.next_attempt_at<=now,
            or_(Turn.status=='pending',(Turn.status=='running')&(Turn.lease_until<now)))
            .order_by(Turn.created_at.desc()).limit(32)))
        selected={}
        for row in rows:
            selected.setdefault((row.user_id,row.world_id,row.channel),row.id)
        pending=list(selected.values())[:4]
    async def batch():
        await asyncio.gather(*(process(identity) for identity in pending),return_exceptions=True)
    asyncio.run(batch())
