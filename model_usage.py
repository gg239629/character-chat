"""Content-free model metrics, queued in memory and persisted in background batches."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import threading
import time
from uuid import uuid4
from sqlalchemy import String,Float,Integer,select,delete
from sqlalchemy.orm import Mapped,mapped_column,Session
import storage

category=ContextVar('model_call_category',default='action')
_LOCK=threading.Lock()
_QUEUE=[]
_LAST={}


class Usage(storage.Base):
    __tablename__='chat_model_usage'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    conversation_id:Mapped[str]=mapped_column(String(64))
    kind:Mapped[str]=mapped_column(String(24),index=True)
    model:Mapped[str]=mapped_column(String(128))
    created_at:Mapped[float]=mapped_column(Float,index=True)
    elapsed_ms:Mapped[int]=mapped_column(Integer)
    input_chars:Mapped[int]=mapped_column(Integer)
    output_chars:Mapped[int]=mapped_column(Integer)
    input_tokens:Mapped[int|None]=mapped_column(Integer,nullable=True)
    output_tokens:Mapped[int|None]=mapped_column(Integer,nullable=True)
    cached_tokens:Mapped[int|None]=mapped_column(Integer,nullable=True)
    success:Mapped[int]=mapped_column(Integer)


@contextmanager
def scope(kind):
    token=category.set(kind)
    try:yield
    finally:category.reset(token)


def config(kind,cfg):
    """Role-agent work uses its own config; all helpers use this story's group."""
    if kind=='suggestion':return role_config('wan')
    if kind in ('character','action','initiative','initiative_public','peer_inquiry','peer_reply','peer_followup','action_voice'):
        return cfg
    return group_config()


def group_config():
    return channel_config('group')


def role_config(role):
    import actors
    if role not in actors.ROLES:raise ValueError('Invalid model role')
    return channel_config(role)


def channel_config(channel):
    import server
    import role_actions
    user,cid=storage.scope.get()
    data=storage.get_store().read(user,cid)
    world=data.get('world_id',cid)
    with role_actions.window(user,world,channel):
        return server.load_config()


def field(value,name):
    return value.get(name) if isinstance(value,dict) else getattr(value,name,None)


async def complete(client,cfg,messages,kind,**options):
    with Measure(kind,cfg,messages) as metric:
        response=await client.chat.completions.create(model=cfg['model'],messages=messages,**options)
        metric.observe(response)
        metric.output(response.choices[0].message.content or '')
        return response


class Measure:
    def __init__(self,kind,cfg,messages):
        self.engine=storage.get_store().engine
        user,conversation=storage.scope.get()
        self.row={'id':uuid4().hex,'user_id':user,'conversation_id':conversation,'kind':kind,
            'model':str(cfg.get('model',''))[:128],'created_at':time.time(),
            'input_chars':len(json.dumps(messages,ensure_ascii=False,separators=(',',':'))),
            'output_chars':0,'input_tokens':None,'output_tokens':None,'cached_tokens':None}
    def observe(self,response):
        usage=field(response,'usage')
        if usage is None:return
        for source,target in [('prompt_tokens','input_tokens'),('completion_tokens','output_tokens')]:
            value=field(usage,source)
            if isinstance(value,int):self.row[target]=value
        cache=field(field(usage,'prompt_tokens_details'),'cached_tokens')
        if not isinstance(cache,int):cache=field(usage,'prompt_cache_hit_tokens')
        if isinstance(cache,int):self.row['cached_tokens']=cache
    def output(self,text):self.row['output_chars']=len(text)
    def __enter__(self):self.start=time.perf_counter();return self
    def __exit__(self,kind,value,tb):
        self.row.update(elapsed_ms=round((time.perf_counter()-self.start)*1000),success=int(kind is None))
        with _LOCK:_QUEUE.append((self.engine,self.row))


def flush(force=False):
    engine=storage.get_store().engine;now=time.time()
    with _LOCK:
        if not force and now-_LAST.get(engine,0)<60:return
        batch=[row for db,row in _QUEUE if db is engine]
        if not batch:return
        with Session(engine) as db:
            db.add_all(Usage(**row) for row in batch)
            db.execute(delete(Usage).where(Usage.created_at<now-30*86400));db.commit()
        _QUEUE[:]=[(db,row) for db,row in _QUEUE if db is not engine]
        _LAST[engine]=now


def report(user,days=7):
    cutoff=time.time()-max(1,min(30,days))*86400
    with Session(storage.get_store().engine) as db:
        rows=[{column.name:getattr(row,column.name) for column in Usage.__table__.columns}
              for row in db.scalars(select(Usage).where(Usage.user_id==user,Usage.created_at>=cutoff))]
    with _LOCK:rows += [dict(row) for engine,row in _QUEUE if engine is storage.get_store().engine and row['user_id']==user and row['created_at']>=cutoff]
    result={}
    for row in rows:
        item=result.setdefault(row['kind'],{'calls':0,'failed':0,'elapsed_ms':0,'input_chars':0,'output_chars':0,
                                           'input_tokens':0,'output_tokens':0,'cached_tokens':0,'calls_with_token_usage':0})
        item['calls']+=1;item['failed']+=not row['success']
        for key in ('elapsed_ms','input_chars','output_chars','input_tokens','output_tokens','cached_tokens'):item[key]+=row[key] or 0
        item['calls_with_token_usage']+=row['input_tokens'] is not None
    return {'days':days,'categories':result,'note':'Token totals cover only calls where the provider returned usage; characters are not token estimates.'}
