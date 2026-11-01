"""Durable post-response scene/archive jobs, isolated from chat generation leases."""
import asyncio
import hashlib
import time
from uuid import uuid4
from sqlalchemy import String, Float, select, update, delete, or_
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
import storage


class Job(storage.Base):
    __tablename__='chat_maintenance_jobs'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id: Mapped[str]=mapped_column(String(128))
    world_id: Mapped[str]=mapped_column(String(64),index=True)
    channel: Mapped[str]=mapped_column(String(8))
    kind: Mapped[str]=mapped_column(String(16))
    through: Mapped[float]=mapped_column(Float)
    status: Mapped[str]=mapped_column(String(16),index=True,default='pending')
    due_at: Mapped[float]=mapped_column(Float,index=True)
    attempts: Mapped[int]=mapped_column(default=0)
    token: Mapped[str]=mapped_column(String(64),default='')
    lease_until: Mapped[float]=mapped_column(Float,default=0)


class Lane(storage.Base):
    __tablename__='chat_maintenance_lanes'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    token: Mapped[str]=mapped_column(String(64),default='')
    expires: Mapped[float]=mapped_column(Float,default=0)


def enqueue(user,world,channel,source,through,scene=True,archive_now=False):
    import actors
    storage.get_store().read(user,world)
    if channel not in ('group',*actors.ROLES): raise ValueError('Invalid channel')
    now=time.time()
    for kind in (['scene','archive'] if scene else ['archive']):
        scheduled=(int(now//60)+1)*60 if kind=='archive' and not archive_now else now
        key=f'minute-{int(now//60)}' if kind=='archive' else source
        identity=hashlib.sha256(f'{user}:{world}:{channel}:{key}:{kind}'.encode()).hexdigest()
        with Session(storage.get_store().engine) as db:
            if db.get(Job,identity):continue
            db.add(Job(id=identity,user_id=user,world_id=world,channel=channel,kind=kind,
                through=through,status='pending',due_at=scheduled,attempts=0,token='',lease_until=0))
            try:db.commit()
            except IntegrityError:db.rollback()


def reserve(identity):
    now=time.time();token=uuid4().hex
    with Session(storage.get_store().engine) as db:
        job=db.get(Job,identity)
        if not job or job.due_at>now or job.attempts>=3 or job.status not in ('pending','running') or (job.status=='running' and job.lease_until>now):return None
        if job.kind=='scene':
            older=db.scalar(select(Job.id).where(Job.user_id==job.user_id,Job.world_id==job.world_id,
                Job.kind=='scene',Job.through<job.through,Job.status=='pending',Job.due_at<=now).limit(1))
            if older:return None
        # Scene work is serialized across all four windows; archives only per window.
        key=hashlib.sha256(f'{job.user_id}:{job.world_id}:{job.kind}:{job.channel if job.kind=="archive" else "world"}'.encode()).hexdigest()
        if not db.get(Lane,key):
            db.add(Lane(id=key,token='',expires=0))
            try:db.commit()
            except IntegrityError:db.rollback()
        result=db.execute(update(Lane).where(Lane.id==key,Lane.expires<=now).values(token=token,expires=now+300))
        if not result.rowcount:db.rollback();return None
        result=db.execute(update(Job).where(Job.id==identity,Job.attempts<3,
            or_(Job.status=='pending',(Job.status=='running') & (Job.lease_until<=now)))
            .values(status='running',attempts=Job.attempts+1,token=token,lease_until=now+300))
        if not result.rowcount:db.rollback();return None
        db.commit()
        return key,token


def finish(identity,lane,token,success):
    now=time.time()
    with Session(storage.get_store().engine) as db:
        row=db.get(Job,identity)
        if row and row.token==token:
            row.status='done' if success else 'failed' if row.attempts>=3 else 'pending'
            row.due_at=now+60;row.lease_until=0
        db.execute(update(Lane).where(Lane.id==lane,Lane.token==token).values(expires=0,token=''))
        db.commit()


def process(identity):
    import server
    import role_actions
    import chat_history
    import long_memory
    claim=reserve(identity)
    if not claim:return
    lane,token=claim;success=False;started=time.monotonic()
    try:
        with Session(storage.get_store().engine) as db:
            row=db.get(Job,identity)
            user,world,channel,kind,through=row.user_id,row.world_id,row.channel,row.kind,row.through
            attempt=row.attempts
        with role_actions.window(user,world,channel):
            if kind=='archive' and not long_memory.pick_batch(storage.read_document('pending_archive',[])):
                success=True;return
            import model_usage
            cfg=model_usage.group_config()
            if server.check_cfg(cfg):raise ValueError('请配置群聊模型以执行后台维护')
            if kind=='scene':
                # An older failed job must never undo a newer applied world update.
                if max(server.shared_world_data().get('scene_applied_at',0),server.shared_world_data().get('world_clock_revision',0))>=through:
                    success=True;return
                rows=chat_history.messages(100)
                history=[{'role':m['role'],'content':m['content']} for m in rows if m['created_at']<=through]
                revision=server.scene_revision.set(through)
                try:
                    success=server.update_scene_state(history,cfg_override=cfg) is not None
                finally:server.scene_revision.reset(revision)
            else:
                batch=long_memory.pick_batch(storage.read_document('pending_archive',[]))
                success=not batch or bool(long_memory.refresh_summary(cfg))
    except Exception as error:
        # Never log model config, conversation text or raw provider errors.
        import task_health
        if 'user' in locals():task_health.record(user,world,identity,kind,error,attempt,int((time.monotonic()-started)*1000))
        success=False
    finally:
        finish(identity,lane,token,success)


def due(limit=12):
    now=time.time()
    with Session(storage.get_store().engine) as db:
        db.execute(update(Job).where(Job.status=='running',Job.lease_until<=now,Job.attempts>=3).values(status='failed'))
        db.execute(delete(Job).where(Job.status.in_(['done','failed']),Job.due_at<now-7*86400))
        db.commit()
        return list(db.scalars(select(Job.id).where(Job.attempts<3,Job.due_at<=now,
            or_(Job.status=='pending',(Job.status=='running') & (Job.lease_until<=now)))
            .order_by(Job.through,Job.kind.desc()).limit(limit)))


class Worker:
    def __init__(self):
        self.wake=asyncio.Event();self.active=set();self.stopping=False;self.task=None

    async def run(self):
        try:
            while not self.stopping:
                self.wake.clear()
                try:
                    import memory_autosave
                    await asyncio.to_thread(memory_autosave.flush_due)
                    import model_usage
                    await asyncio.to_thread(model_usage.flush)
                    for identity in await asyncio.to_thread(due):
                        if len(self.active)>=4:break
                        if any(getattr(task,'job_id',None)==identity for task in self.active):continue
                        task=asyncio.create_task(asyncio.to_thread(process,identity))
                        task.job_id=identity;self.active.add(task)
                        def completed(task):
                            self.active.discard(task)
                            if not task.cancelled():task.exception()
                        task.add_done_callback(completed)
                except Exception:pass
                try:await asyncio.wait_for(self.wake.wait(),timeout=1)
                except asyncio.TimeoutError:pass
        except asyncio.CancelledError:pass

    def start(self):self.task=asyncio.create_task(self.run())

    async def stop(self):
        self.stopping=True;self.wake.set()
        if self.task:await self.task
        # Running I/O is allowed to finish on graceful shutdown; jobs survive a hard stop.
        if self.active:await asyncio.gather(*self.active,return_exceptions=True)
        import memory_autosave
        await asyncio.to_thread(memory_autosave.flush_due,force=True)
        import model_usage
        await asyncio.to_thread(model_usage.flush,force=True)
