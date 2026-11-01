"""Content-free diagnostics for background work; no provider URLs or messages."""
import time
from uuid import uuid4
from sqlalchemy import String, Float, Integer, select, delete
from sqlalchemy.orm import Mapped, mapped_column, Session
import storage


class Failure(storage.Base):
    __tablename__='chat_task_failures'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    world_id:Mapped[str]=mapped_column(String(64),index=True)
    task_id:Mapped[str]=mapped_column(String(64))
    stage:Mapped[str]=mapped_column(String(24))
    error_type:Mapped[str]=mapped_column(String(80))
    created_at:Mapped[float]=mapped_column(Float)
    attempts:Mapped[int]=mapped_column(Integer,default=0)
    elapsed_ms:Mapped[int]=mapped_column(Integer,default=0)


def record(user,world,task,stage,error,attempts=0,elapsed_ms=0):
    try:
        with Session(storage.get_store().engine) as db:
            db.add(Failure(id=uuid4().hex,user_id=user,world_id=world,task_id=task[:64],stage=stage[:24],
                error_type=type(error).__name__[:80],created_at=time.time(),attempts=max(0,int(attempts)),elapsed_ms=max(0,int(elapsed_ms))))
            db.execute(delete(Failure).where(Failure.created_at<time.time()-30*86400));db.commit()
    except Exception:pass


def report(user,world):
    storage.get_store().read(user,world)
    with Session(storage.get_store().engine) as db:
        return [{'task_id':r.task_id,'stage':r.stage,'error_type':r.error_type,'created_at':r.created_at,'attempts':r.attempts,'elapsed_ms':r.elapsed_ms} for r in db.scalars(
            select(Failure).where(Failure.user_id==user,Failure.world_id==world).order_by(Failure.created_at.desc()).limit(30))]


def retry(user,world,stage,identity):
    import maintenance
    import peer_chat
    import chat_inbox
    import role_actions
    storage.get_store().read(user,world)
    model=(chat_inbox.Turn if stage in ('reply','model_reply') else maintenance.Job if stage in ('scene','archive')
        else peer_chat.Job if stage in ('peer_reply','peer_inquiry','peer_followup')
        else role_actions.Action if stage=='action' else None)
    if model is None:raise ValueError('此任务类型不支持手动重试')
    from sqlalchemy import update
    with Session(storage.get_store().engine) as db:
        row=db.get(model,identity)
        if not row or (row.user_id,row.world_id)!=(user,world):raise PermissionError('Task unavailable')
        if row.status!='failed':raise ValueError('仅完全失败的任务可以重试')
        if model is chat_inbox.Turn:
            channel=row.channel
        else:
            values={'status':'pending','attempts':0,'due_at':time.time(),'lease_until':0}
            if model is not role_actions.Action:values['token']=''
            result=db.execute(update(model).where(model.id==identity,model.status=='failed').values(**values))
            if not result.rowcount:raise ValueError('任务状态已变化，请刷新')
            db.commit();return {'status':'pending'}
    with role_actions.window(user,world,channel):return chat_inbox.retry(identity)
