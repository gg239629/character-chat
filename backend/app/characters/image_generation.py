"""Durable, owner-scoped image tasks and image messages; no automatic paid retries."""
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from sqlalchemy import String,JSON,Float,LargeBinary,select,update,func
from sqlalchemy.orm import Mapped,mapped_column,Session
from sqlalchemy.exc import IntegrityError
from backend.app.infrastructure import storage,secrets_store,events
from backend.app.characters import actors

MAX_BYTES=20*1024*1024
SIZES=('1024x1024','1024x1536','1536x1024','auto')


class Settings(storage.Base):
    __tablename__='chat_image_settings'
    user_id:Mapped[str]=mapped_column(String(128),primary_key=True)
    settings:Mapped[dict]=mapped_column(JSON)


class Job(storage.Base):
    __tablename__='chat_image_jobs'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    world_id:Mapped[str]=mapped_column(String(64),index=True)
    channel:Mapped[str]=mapped_column(String(8))
    actor:Mapped[str]=mapped_column(String(8),default='')
    prompt:Mapped[str]=mapped_column(String(4000))
    size:Mapped[str]=mapped_column(String(16))
    model:Mapped[str]=mapped_column(String(200))
    status:Mapped[str]=mapped_column(String(16),index=True,default='pending')
    created_at:Mapped[float]=mapped_column(Float)
    lease_until:Mapped[float]=mapped_column(Float,default=0)
    error:Mapped[str]=mapped_column(String(500),default='')
    image:Mapped[bytes|None]=mapped_column(LargeBinary,nullable=True,deferred=True)
    mime:Mapped[str]=mapped_column(String(32),default='')
    message_id:Mapped[str]=mapped_column(String(64),default='')


def settings(user):
    with Session(storage.get_store().engine) as db:row=db.get(Settings,user)
    return secrets_store.decode_settings(row.settings) if row else {
        'enabled':False,'inherit_connection':True,'model':'gpt-image-2.5','base_url':'','api_key':''}


def public_settings(user):
    cfg=settings(user)
    return {k:v for k,v in cfg.items() if k!='api_key'}|{'has_key':bool(cfg.get('api_key'))}


def save_settings(user,body):
    cfg=settings(user)
    for key in ('enabled','inherit_connection','model','base_url'):cfg[key]=body[key]
    if body.get('api_key'):cfg['api_key']=body['api_key']
    if cfg['enabled'] and not cfg['model']:raise ValueError('请填写图片模型名称')
    if cfg['enabled'] and not cfg['inherit_connection'] and not (cfg['api_key'] and cfg['base_url']):
        raise ValueError('请填写生图接口地址和密钥，或选择沿用当前聊天连接')
    if cfg.get('base_url'):
        parsed=urlsplit(cfg['base_url'])
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('接口地址须为 http 或 https 地址，不应包含用户名或密码')
    with Session(storage.get_store().engine) as db:
        row=db.get(Settings,user)
        if not row:row=Settings(user_id=user);db.add(row)
        row.settings=secrets_store.encode_settings(cfg);db.commit()
    return public_settings(user)


def connection(user):
    from backend.app.core import services
    cfg=settings(user)
    if not cfg.get('enabled'):raise ValueError('请先在主页设置的“生图”中启用并配置图片模型')
    if cfg.get('inherit_connection'):
        text=services.models().load_config()
        cfg.update(base_url=text.get('base_url',''),api_key=text.get('api_key',''))
    if not cfg.get('model') or not cfg.get('api_key'):raise ValueError('请先配置生图模型和可用密钥')
    return cfg


def info(row):
    return {k:getattr(row,k) for k in ('id','status','actor','prompt','size','model','error','message_id','created_at')}


def enqueue(user,world,channel,actor,prompt,size,request_id):
    from backend.app.conversations import message_service
    from backend.app.runtime import role_actions
    if channel not in ('group',*actors.ROLES):raise ValueError('无效聊天窗口')
    if actor not in ('',*actors.ROLES) or (channel!='group' and actor!=channel):raise ValueError('请使用当前聊天的角色')
    if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=4000:raise ValueError('图片描述需为 1–4000 字')
    if size not in SIZES:raise ValueError('不支持的图片尺寸')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):raise ValueError('无效请求标识')
    identity=hashlib.sha256(f'{user}:{world}:{channel}:{request_id}'.encode()).hexdigest()
    with role_actions.window(user,world,channel):
        with Session(storage.get_store().engine) as db:
            existing=db.get(Job,identity)
            if existing:
                if (existing.prompt,existing.actor,existing.size)!=(prompt.strip(),actor,size):raise ValueError('同一次请求不能更改图片描述')
                return info(existing)
        cfg=connection(user)
        cid=storage.scope.get()[1]
        from backend.app.memory import memory_autosave
        memory_autosave.flush(user,cid,force=True)
        for attempt in range(5):
            try:
                with Session(storage.get_store().engine) as db:
                    previous=db.get(Job,identity)
                    if previous:
                        if (previous.prompt,previous.actor,previous.size)!=(prompt.strip(),actor,size):raise ValueError('同一次请求不能更改图片描述')
                        return info(previous)
                    count=db.scalar(select(func.count()).select_from(Job).where(Job.user_id==user,Job.status.in_(['pending','running'])))
                    if count>=3:raise ValueError('已有图片正在生成，请稍后再试')
                    row=Job(id=identity,user_id=user,world_id=world,channel=channel,actor=actor,
                        prompt=prompt.strip(),size=size,model=cfg['model'],status='pending',created_at=time.time(),lease_until=0,error='',mime='',message_id='')
                    db.add(row)
                    message_service.write(db,user,cid,'image-'+identity,0,'user','[生图请求] '+prompt.strip())
                    db.commit();result=info(row)
                break
            except (IntegrityError,RuntimeError):
                if attempt==4:raise
    events.publish(user)
    return result


def get(user,world,channel,identity):
    storage.get_store().read(user,world)
    with Session(storage.get_store().engine) as db:
        row=db.get(Job,identity)
        if not row or (row.user_id,row.world_id,row.channel)!=(user,world,channel):raise PermissionError('图片任务不存在')
        return info(row)


def recent(user,world,channel):
    storage.get_store().read(user,world)
    with Session(storage.get_store().engine) as db:
        return [info(row) for row in db.scalars(select(Job).where(Job.user_id==user,Job.world_id==world,Job.channel==channel,
            Job.status!='done').order_by(Job.created_at.desc()).limit(6))]


def content(user,identity):
    with Session(storage.get_store().engine) as db:
        row=db.get(Job,identity)
        if not row or row.user_id!=user or row.status!='done' or not row.image:raise PermissionError('图片不存在')
        storage.get_store().read(user,row.world_id)
        return row.image,row.mime


def visual_prompt(user,world,channel,actor,prompt):
    from backend.app.conversations import chat_history
    from backend.app.world import world_time
    data=storage.get_store().read(user,world)
    appearance=''
    if actor:
        path=Path(__file__).resolve().parents[3]/'memory'/f'{actor}.md'
        text=path.read_text(encoding='utf-8') if path.exists() else ''
        # Select visual traits only; do not forward the character's full behavior
        # contract, unrelated private memories or other characters' conversations.
        lines=[line for line in text.splitlines() if any(k in line for k in ('外形基调','发型','发色','穿衣','常服','眼睛'))]
        appearance='\n'.join(lines)[:700]
    context={'人物':actors.ROLES.get(actor,'场景'),'外观参考':appearance,
        '当前时间':world_time.display(world_time.clock(data)),
        '本人状态':data.get('role_states',{}).get(actor,{}).get('last_confirmed_status',data.get('role_states',{}).get(actor,{}).get('status','未知')),
        '当前窗口最近对话':[{'role':m['role'],'text':re.sub(r'\[\[image:[0-9a-f]{64}\]\]','[图片]',m['content'])[:500]} for m in chat_history.messages(6)]}
    return '根据用户要求生成一张图片。用户明确描述优先；人物为成年虚构角色。参考仅用于视觉连续性，不要把上下文文字或界面画进图片。\n用户要求：'+prompt+'\n视觉参考：'+json.dumps(context,ensure_ascii=False)


def decode_image(item,base_url):
    data=None
    if getattr(item,'b64_json',None):
        encoded=item.b64_json
        if len(encoded)>MAX_BYTES*4//3+8:raise ValueError('图片超过 20MB，请降低尺寸')
        data=base64.b64decode(encoded,validate=True)
    elif getattr(item,'url',None):
        import httpx
        from backend.app.infrastructure.model_transport import is_local_gateway
        url=item.url;parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname:raise ValueError('图片服务返回了无效地址')
        if parsed.scheme=='http' and parsed.hostname!=urlsplit(base_url or '').hostname:raise ValueError('图片服务须返回安全的图片地址')
        with httpx.Client(timeout=45,trust_env=not is_local_gateway(url),follow_redirects=False) as client:
            with client.stream('GET',url) as response:
                response.raise_for_status();parts=[];length=0
                for part in response.iter_bytes():
                    length+=len(part)
                    if length>MAX_BYTES:raise ValueError('图片超过 20MB，请降低尺寸')
                    parts.append(part)
                data=b''.join(parts)
    if not data or len(data)>MAX_BYTES:raise ValueError('图片服务没有返回可保存的图片')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
    elif data.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
    elif data.startswith(b'RIFF') and data[8:12]==b'WEBP':mime='image/webp'
    else:raise ValueError('图片服务返回的格式不受支持，请使用 PNG、JPEG 或 WebP')
    return data,mime


def due():
    now=time.time()
    with Session(storage.get_store().engine) as db:
        expired=list(db.scalars(select(Job).where(Job.status=='running',Job.lease_until<now)))
        for row in expired:
            row.status='failed';row.error='生图任务已中断，请重新生成；原请求可能已产生费用'
        db.commit()
        return list(db.scalars(select(Job.id).where(Job.status=='pending').order_by(Job.created_at).limit(2)))


def process(identity):
    from backend.app.runtime import role_actions
    from backend.app.conversations import message_service
    from backend.app.infrastructure.model_transport import client_options
    store=storage.get_store();now=time.time()
    with Session(store.engine) as db:
        result=db.execute(update(Job).where(Job.id==identity,Job.status=='pending').values(status='running',lease_until=now+600))
        if not result.rowcount:db.rollback();return
        db.commit();row=db.get(Job,identity)
        user,world,channel,actor,prompt,size,model=row.user_id,row.world_id,row.channel,row.actor,row.prompt,row.size,row.model
    events.publish(user)
    try:
        with role_actions.window(user,world,channel):
            cfg=connection(user)
            from openai import OpenAI
            from backend.app.infrastructure import model_usage
            # Disable SDK retries for paid generation: an uncertain network result
            # must not silently generate and charge for another image.
            with OpenAI(api_key=cfg['api_key'],base_url=cfg.get('base_url') or None,timeout=300,max_retries=0,**client_options(cfg.get('base_url'))) as client:
                full_prompt=visual_prompt(user,world,channel,actor,prompt)
                with model_usage.Measure('image_generation',{**cfg,'model':model},[{'role':'user','content':full_prompt}]) as metric:
                    response=client.images.generate(model=model,prompt=full_prompt,n=1,size=size)
                    metric.observe(response)
            if not response.data:raise ValueError('图片服务没有返回图片')
            image,mime=decode_image(response.data[0],cfg.get('base_url'))
            cid=storage.scope.get()[1]
            from backend.app.memory import memory_autosave
            memory_autosave.flush(user,cid,force=True)
            for attempt in range(5):
                try:
                    with Session(store.engine) as db:
                        row=db.get(Job,identity)
                        if not row or row.status!='running':return
                        speaker=actors.ROLES.get(actor,'图片')
                        message,_=message_service.write(db,user,cid,'image-'+identity,1,'assistant',
                            speaker+'：'+prompt+'\n[[image:'+identity+']]',speaker)
                        row.image=image;row.mime=mime;row.status='done';row.message_id=message.id;row.lease_until=0
                        db.commit()
                    break
                except RuntimeError:
                    if attempt==4:raise
    except Exception as error:
        status=getattr(error,'status_code',None)
        if isinstance(error,ValueError):message=str(error)[:300]
        elif status in (401,403):message='生图密钥无效或没有图片模型权限，请检查生图设置'
        elif status==404:message='生图接口或模型不存在，请确认该服务支持 /images/generations'
        elif status==429:message='生图服务限流或额度不足，请稍后重新生成'
        else:message='生图未完成，请检查接口和图片模型后重试（'+type(error).__name__+(f' / {status}' if status else '')+'）'
        with Session(store.engine) as db:
            db.execute(update(Job).where(Job.id==identity,Job.status=='running').values(status='failed',error=message,lease_until=0));db.commit()
    finally:events.publish(user)
