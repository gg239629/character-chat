"""Durable asynchronous voice replies, tied to the original character message."""
import asyncio
import hashlib
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from sqlalchemy import String,Float,LargeBinary,select,update
from sqlalchemy.orm import Mapped,mapped_column,Session
from sqlalchemy.exc import IntegrityError
from backend.app.infrastructure import storage,events
from backend.app.characters import actors,speech_settings
from backend.app.core.context import chat_world

TOOL={'type':'function','function':{'name':'send_voice','description':
    '用语音回答。私聊中用户想听声音或情绪、语气重要的短交流可自然选择；长说明、数字地址清单和连续快聊优先文字。群聊仅在最新用户明确要求语音时调用，能力询问、引用和否定不算请求。只写实际说出口的话，括号动作不朗读。',
    'parameters':{'type':'object','properties':{'text':{'type':'string','description':'角色实际说出口的台词，不含括号动作、提示词或内部标记。'},
        'style':{'type':'string','description':'当下情绪、语速、停顿与语气指导；保持基础音色，不改变人物声音身份。'}},'required':['text','style'],'additionalProperties':False}}}
INSTRUCTIONS='可以在合适时调用 send_voice 用语音回复，不必每次使用。台词放text，情绪和停顿放style，不在聊天里输出工具标记。不调用工具就正常文字回复；群聊仅在最新用户明确要求语音时使用。'
EXECUTOR=ThreadPoolExecutor(max_workers=2,thread_name_prefix='chat-voice')
INFLIGHT=set()
LOCK=threading.Lock()


class Job(storage.Base):
    __tablename__='chat_voice_jobs'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    world_id:Mapped[str]=mapped_column(String(64),index=True)
    channel:Mapped[str]=mapped_column(String(8))
    actor:Mapped[str]=mapped_column(String(8))
    text:Mapped[str]=mapped_column(String(4000))
    style:Mapped[str]=mapped_column(String(1000))
    voice:Mapped[str]=mapped_column(String(200))
    status:Mapped[str]=mapped_column(String(16),index=True,default='pending')
    created_at:Mapped[float]=mapped_column(Float)
    lease_until:Mapped[float]=mapped_column(Float,default=0)
    error:Mapped[str]=mapped_column(String(500),default='')
    audio:Mapped[bytes|None]=mapped_column(LargeBinary,nullable=True,deferred=True)
    mime:Mapped[str]=mapped_column(String(64),default='')


def spoken(text):
    text=re.sub(r'^\s*小[婉研柳][：:]\s*','',text)
    # Balanced nesting handles both Chinese and ASCII action parentheses.
    out=[];depth=0
    for char in text:
        if char in '（(':depth+=1
        elif char in '）)':depth=max(0,depth-1)
        elif not depth:out.append(char)
    return re.sub(r'\[\[[\s\S]*?\]\]','',''.join(out)).strip()


def group_request(text):
    if re.search(r'不要|不用|别发|不想听|不需要',text):return False
    if re.search(r'(?:能|会|支持|可以).{0,10}(?:语音|声音).*[吗么？?]',text) and not re.search(r'能不能|可以不可以|可不可以',text):return False
    return bool(re.search(r'(?:发|来|用|说|听|要).{0,20}(?:语音|声音)|(?:语音|声音).{0,20}(?:回复|回答|发来)|(?:读|念).{0,12}(?:给我|一下|出来)',text))


def available(user,world,channel,actor,current):
    if channel=='group' and not group_request(current):return False
    if not speech_settings.read(user).get('enabled'):return False
    with actors.role_scope(user,world,actor):
        return bool(storage.read_document('base_voice','').strip()) and (channel=='group' or storage.read_document('reply_mode','text')=='voice')


def info(row):return {'id':row.id,'status':row.status,'error':row.error}


def enqueue(request,actor,calls,current):
    world=chat_world.get();user=storage.scope.get()[0]
    if actor not in actors.ROLES or (world['channel']!='group' and world['channel']!=actor):raise ValueError('语音人物不匹配')
    if not available(user,world['id'],world['channel'],actor,current):raise ValueError('当前消息不可使用语音，请检查开关和角色音色')
    if len(calls)!=1:raise ValueError('每条回复只支持一条语音')
    call=next(iter(calls.values()))
    if call['name']!='send_voice':raise ValueError('语音工具无效')
    args=json.loads(call['arguments'])
    if not isinstance(args,dict) or not isinstance(args.get('text'),str) or not isinstance(args.get('style',''),str):raise ValueError('语音参数无效')
    text=spoken(args['text']);style=args.get('style','').strip()
    if not 1<=len(text)<=4000 or len(style)>1000:raise ValueError('语音内容长度无效')
    with actors.role_scope(user,world['id'],actor):voice=storage.read_document('base_voice','').strip()
    identity=hashlib.sha256(f"{user}:{world['id']}:{world['channel']}:{actor}:{request}".encode()).hexdigest()
    with Session(storage.get_store().engine) as db:
        old=db.get(Job,identity)
        if old:return {**info(old),'text':old.text}
        row=Job(id=identity,user_id=user,world_id=world['id'],channel=world['channel'],actor=actor,text=text,style=style,
            voice=voice,status='draft',created_at=time.time(),lease_until=0,error='',mime='')
        db.add(row)
        try:db.commit()
        except IntegrityError:db.rollback();row=db.get(Job,identity)
        result={**info(row),'text':row.text}
    events.publish(user);return result


def get(user,world,channel,identity):
    with Session(storage.get_store().engine) as db:
        row=db.get(Job,identity)
        if not row or (row.user_id,row.world_id,row.channel)!=(user,world,channel):raise PermissionError('语音不存在')
        return info(row)


def content(user,identity):
    with Session(storage.get_store().engine) as db:
        row=db.get(Job,identity)
        if not row or row.user_id!=user or row.status!='done':raise PermissionError('语音不可用')
        return row.audio,row.mime


def due():
    now=time.time()
    with Session(storage.get_store().engine) as db:
        db.execute(update(Job).where(Job.status=='running',Job.lease_until<now).values(status='failed',error='语音生成中断，文字仍可查看',lease_until=0));db.commit()
        return list(db.scalars(select(Job.id).where(Job.status=='pending').order_by(Job.created_at).limit(2)))


def process(identity):
    with Session(storage.get_store().engine) as db:
        claimed=db.execute(update(Job).where(Job.id==identity,Job.status=='pending').values(status='running',lease_until=time.time()+180))
        if not claimed.rowcount:db.rollback();return
        db.commit();row=db.get(Job,identity)
        user,world,channel,actor,text,style,voice=row.user_id,row.world_id,row.channel,row.actor,row.text,row.style,row.voice
    events.publish(user)
    try:
        from backend.app.runtime import role_actions
        with role_actions.window(user,world,channel):
            if not speech_settings.read(user).get('enabled'):raise ValueError('语音已关闭')
            cfg=speech_settings.connection(user)
            data,mime=asyncio.run(speech_settings.synthesize(cfg,voice,text,style))
        with Session(storage.get_store().engine) as db:
            row=db.get(Job,identity)
            if row and row.status=='running':row.audio=data;row.mime=mime;row.status='done';row.lease_until=0;db.commit()
    except Exception:
        with Session(storage.get_store().engine) as db:
            row=db.get(Job,identity)
            if row and row.status=='running':row.status='failed';row.error='语音未能生成，文字仍可查看';row.lease_until=0;db.commit()
    finally:events.publish(user)


def attach(job,text):
    value=spoken(text)
    with Session(storage.get_store().engine) as db:
        row=db.get(Job,job['id'])
        if not row or row.user_id!=storage.scope.get()[0]:raise PermissionError('语音不存在')
        if row.status in ('draft','pending'):row.text=value;row.status='pending';db.commit()
        job['status']=row.status
        value=row.text
    # Keep action narration for state extraction; only the audio/transcript omit it.
    return text.rstrip()+'\n[[voice:'+job['id']+']]'


def start(job):
    if job['status']!='pending':return
    with LOCK:
        if job['id'] in INFLIGHT:return
        INFLIGHT.add(job['id'])
    task=EXECUTOR.submit(copy_context().run,process,job['id'])
    def done(future):
        with LOCK:INFLIGHT.discard(job['id'])
        future.exception()
    task.add_done_callback(done)
