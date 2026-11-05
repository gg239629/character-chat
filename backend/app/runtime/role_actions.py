"""Durable role intentions, cross-channel delivery and personality-driven contact."""
import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import time
from uuid import uuid4
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, Float, JSON, select, update, func, or_
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.infrastructure import storage
from backend.app.infrastructure import model_usage
from backend.app.infrastructure import model_json

_NO_JSON_MODE=set()


class Behavior(BaseModel):
    """Internal capabilities; old database switches and limits are intentionally ignored."""
    enabled: bool = True
    allow_private: bool = True
    allow_group: bool = True
    allow_peers: bool = True
    respond_to_roles: bool = True
    confidentiality: str = '尊重秘密和隐私，不擅自转述'



class Action(storage.Base):
    __tablename__ = 'chat_role_actions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    world_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(8))
    target: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), index=True)
    source: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
    due_at: Mapped[float] = mapped_column(Float, index=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(default=0)


class Checkpoint(storage.Base):
    __tablename__ = 'chat_role_checkpoints'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_check: Mapped[float] = mapped_column(Float)


@contextmanager
def window(user, world_id, channel):
    """Enter a validated owner/story/channel without inheriting request snapshots."""
    from backend.app.core import context as execution_context, services
    world=storage.get_store().read(user, world_id)
    if world.get('world_id'):
        raise PermissionError('A private channel cannot be used as a story')
    from backend.app.world import realtime_world
    world=realtime_world.sync(user,world_id,world)
    from backend.app.conversations import message_service
    identity=message_service.conversation(user,world_id,channel)
    token=storage.scope.set((user,identity))
    snapshot=storage.request_snapshot.set(storage.load_request_data(user,identity))
    world_token=execution_context.chat_world.set({'id':world_id,'channel':channel,'data':world})
    try:
        yield identity
    finally:
        execution_context.chat_world.reset(world_token)
        storage.request_snapshot.reset(snapshot)
        storage.scope.reset(token)


def settings():
    return Behavior()


def cross_target(text, actor, channel=None):
    import re
    if re.search(r'(?:吗|没有|没|[?？])',text) and re.match(r'^\s*(?:小婉|小研|小柳).{0,16}(?:叫|喊|发|说|告诉)',text) and not re.search(r'帮我|替我|请你|你去|你帮|你问|你发',text):return None
    if not re.search(r'发|说|问|告诉|转告|转发|联系|通知|聊|私信|叫|喊',text):
        return None
    delegated=[key for key,name in actors.ROLES.items() if key!=actor and
        re.search(r'(?:让|叫|请|催)'+name+r'.{0,16}(?:给我|私信我|联系我|发个|发条)',text)]
    if len(delegated)==1:return delegated[0]
    if channel=='group' and re.search(r'(?:私信|私聊|单独|悄悄|单聊|私下).{0,12}(?:我|给我|发)|(?:给我|向我).{0,8}(?:私信|私聊|单聊)',text):
        return 'user'
    if re.search(r'群里|群聊|群中|群内|大家一起',text):
        return 'group' if channel!='group' else None
    if channel!='group' and re.search(r'(?:叫|喊|通知).{0,5}(?:他们|她们|大家|你们).{0,8}(?:吃饭|吃面|来吃|开饭)',text):return 'group'
    names=[key for key,name in actors.ROLES.items() if key!=actor and (
        re.search(r'(?:告诉|通知|转告|转发|联系|私信|问问?|叫|喊|发给|让|请|催).{0,3}'+name,text)
        or re.search(r'(?:给|向|跟|和)'+name+r'.{0,8}(?:说|聊|发|问)',text))]
    return names[0] if len(names)==1 else None


def resolve_target(messages,actor,channel):
    import re
    users=[m.get('content','') for m in messages if m.get('role')=='user']
    if not users:return None
    current=users[-1]
    direct=cross_target(current,actor,channel)
    if direct:return direct
    if not re.search(r'没(?:有)?(?:看见|看到|收到)|再发|重发|还没发|发了吗|说了吗|说了没|通知了吗|叫他们|叫她们|叫大家|你叫|通知他们|跟[她他].{0,8}说|让[她他].{0,8}发|发个.{1,8}(?:就行|即可)|快去|快点|快说',current):return None
    for previous in reversed(users[-12:-1]):
        target=cross_target(previous,actor,channel)
        if target:return target
    return None


def group_request_roles(text):
    import re
    addressed=next((role for role,name in actors.ROLES.items() if re.match(r'^\s*'+name+r'[，,:：\s你]',text)),None)
    if addressed and cross_target(text,addressed,'group') in actors.ROLES:return [addressed]
    if cross_target(text,'wan','group')!='user':return []
    named=[role for role,name in actors.ROLES.items() if name in text]
    return named or list(actors.ROLES)


async def ask_json(cfg, system, context):
    from backend.app.core import context as execution_context, services
    kind=model_usage.category.get()
    cfg=model_usage.config(kind,cfg)
    messages=[{'role':'system','content':system},{'role':'user','content':json.dumps(context,ensure_ascii=False,separators=(',',':'))}]
    client=services.models().make_client(cfg)
    try:
        with model_usage.Measure(kind,cfg,messages) as metric:
            options={'model':cfg['model'],'temperature':.3,'max_tokens':120 if kind=='coordinator' else 1800 if kind=='archive' else 900,
                'messages':messages}
            capability=(cfg.get('base_url',''),cfg['model'])
            if capability not in _NO_JSON_MODE:options['response_format']={'type':'json_object'}
            try:response=await client.chat.completions.create(**options)
            except Exception as error:
                if getattr(error,'status_code',None) in (400,422) and any(s in str(error).lower() for s in ('response_format','json_object','json mode','json_mode')):
                    if len(_NO_JSON_MODE)>128:_NO_JSON_MODE.clear()
                    _NO_JSON_MODE.add(capability);options.pop('response_format',None)
                    response=await client.chat.completions.create(**options)
                else:raise
            metric.observe(response)
            raw=(response.choices[0].message.content or '').strip()
            metric.output(raw)
            if getattr(response.choices[0],'finish_reason',None)=='length':
                raise model_json.ModelOutputTruncated('JSON response reached output limit')
            return model_json.decode(raw)
    finally:
        await client.close()


def insert_action(user,world,actor,target,kind,source,decision,now=None):
    now=time.time() if now is None else now
    identity=hashlib.sha256(f'{user}:{world}:{actor}:{kind}:{source}'.encode()).hexdigest()
    choice=decision.get('decision','none')
    if choice not in ('send','refuse','ask','later','none'):
        raise ValueError('角色行动决定无效')
    content=str(decision.get('message','')).strip()
    if choice in ('send','later'):
        if not content or len(content)>3000:
            raise ValueError('角色行动消息无效')
        content=actors.normalize_reply(content,actors.ROLES[actor])
    delay=decision.get('delay_minutes',30)
    delay=max(1,min(1440,int(delay))) if choice=='later' else 0
    detail={'message':content,'reason':str(decision.get('reason',''))[:500],
        'sent_reply':str(decision.get('sent_reply',''))[:600],
        'reply':str(decision.get('reply',''))[:1500], 'audience':['user']+([*actors.ROLES] if target=='group' else [actor] if target=='user' else [actor,target]),
        'disclosure':'user-authorized-minimum' if kind=='requested' else 'public-only' if target!='user' else 'actor-private',
        'decision':choice}
    if target in actors.ROLES:
        if target==actor:raise ValueError('Cannot send a peer message to self')
        detail['audience']=[actor,target]
        detail['disclosure']='participants-only'
    if kind=='autonomous' and target in ('user','group'):
        from backend.app.characters import image_tools
        image_prompt=decision.get('image_prompt') or image_tools.legacy_photo(content)
        if isinstance(image_prompt,str) and image_prompt.strip() and len(image_prompt)<=4000:
            detail['image_prompt']=image_prompt.strip()
        detail['message']=image_tools.visible_text(content)
    physical=decision.get('_physical_event')
    if isinstance(physical,dict):detail['physical_event']=physical
    task=decision.get('_recipient_task')
    if kind=='requested' and target in actors.ROLES and isinstance(task,dict) and task.get('type') in ('contact_user','contact_group'):
        detail['recipient_task']={'type':task['type'],'message_hint':str(task.get('message_hint',''))[:400]}
    row=Action(id=identity,user_id=user,world_id=world,actor=actor,target=target,kind=kind,source=source,
        status='pending' if choice in ('send','later') else choice,detail=detail,created_at=now,due_at=now+delay*60,lease_until=0,attempts=0)
    with Session(storage.get_store().engine) as db:
        try:
            db.add(row);db.commit()
        except IntegrityError:
            db.rollback()
        return action_data(db.get(Action,identity))


def action_data(row):
    return {'id':row.id,'actor':row.actor,'target':row.target,'kind':row.kind,'status':row.status,
        'source':row.source,'detail':row.detail,'created_at':row.created_at,'due_at':row.due_at}


def audit(user,world,actor=None,internal=False):
    with Session(storage.get_store().engine) as db:
        query=select(Action).where(Action.user_id==user,Action.world_id==world)
        if not internal:query=query.where(Action.target.in_(['user','group']))
        if actor:
            query=query.where(Action.actor==actor)
        return [action_data(row) for row in db.scalars(query.order_by(Action.created_at.desc()).limit(50))]


def received_contact_status(user,world,actor,query):
    """Answer receipt questions from actual records, not a model's recollection."""
    import re
    from backend.app.conversations import peer_chat
    if not re.search(r'是不是|让你|叫你|谁让|谁叫|转告|收到|发了吗',query):return None
    if not re.search(r'发.{0,6}(?:消息|信息|个|条)|(?:让你|叫你).{0,12}发|转告|谁让|谁叫|收到.{0,5}消息',query):return None
    if not re.search(r'发.{0,6}(?:消息|信息|个|条)|(?:让你|叫你).{0,12}发|转告|谁让|谁叫|收到.{0,5}消息',query):return None
    with Session(storage.get_store().engine) as db:
        rows=db.execute(select(peer_chat.Message,Action).join(Action,Action.id==peer_chat.Message.event_id)
            .where(peer_chat.Message.user_id==user,peer_chat.Message.world_id==world,peer_chat.Message.recipient==actor,
                Action.user_id==user,Action.world_id==world,Action.kind=='requested')
            .order_by(peer_chat.Message.created_at.desc()).limit(12)).all()
        for message,origin in rows:
            if not origin.detail.get('recipient_task'):continue
            from backend.app.conversations import action_contract
            destination=action_contract.destination(origin.detail['recipient_task']) or 'user'
            sender=actors.ROLES[message.sender]
            named=[name for name in actors.ROLES.values() if name in query]
            if named and sender not in named:continue
            contact=db.scalar(select(Action).where(Action.user_id==user,Action.world_id==world,
                Action.actor==actor,Action.target==destination,Action.kind=='delegated',Action.source==message.id).limit(1))
            status=contact.status if contact else 'unanswered'
            body={'sent':'我已经给你发过消息了。','pending':'我决定稍后联系你，还没有发出。',
                'refuse':'不过我这次不想发。','none':'我暂时没发。','failed':'但刚才发送没有成功。',
                'unanswered':'我收到了这个请求，还没有实际给你发送。'}.get(status,'我还没有确认发送。')
            if destination=='group':body=action_contract.result_text('我',destination,status)
            return sender+'确实通过私信把你的请求转给我了。'+body
    return None


async def requested(actor, target, messages, source):
    """Only the human's current request permits disclosure; refusal is a real outcome."""
    from backend.app.core import context as execution_context, services
    user=storage.scope.get()[0]
    world=execution_context.chat_world.get()['id']
    origin=execution_context.chat_world.get()['channel']
    with actors.role_scope(user,world,actor):
        cfg=services.models().load_config()
        behavior=settings()
        persona=storage.read_document('persona','')
        relationship=storage.read_document('relationship','')
    with Session(storage.get_store().engine) as db:
        identity=hashlib.sha256(f'{user}:{world}:{actor}:requested:{source}'.encode()).hexdigest()
        existing=db.get(Action,identity)
        if existing:
            return action_data(existing)
        current=next((m['content'] for m in reversed(messages) if m['role']=='user'),'')
        import re
        if re.search(r'说了吗|说了没|通知了吗|发了吗|怎么没收到|没(?:有)?收到',current):
            previous=db.scalar(select(Action).where(Action.user_id==user,Action.world_id==world,Action.actor==actor,
                Action.target==target,Action.kind=='requested',Action.created_at>time.time()-1800)
                .order_by(Action.created_at.desc()).limit(1))
            if previous:
                result=action_data(previous)
                if target in actors.ROLES and previous.status=='sent':
                    from backend.app.conversations import action_contract
                    destination=action_contract.destination(previous.detail.get('recipient_task')) or 'user'
                    contact=db.scalar(select(Action).where(Action.user_id==user,Action.world_id==world,
                        Action.actor==target,Action.target==destination,Action.kind=='delegated',
                        Action.source==previous.detail.get('message_id','')).limit(1))
                    name=actors.ROLES[target]
                    report=(name+'已经实际给你发送过消息。' if contact and contact.status=='sent' else
                        name+'决定稍后联系你，目前尚未发送。' if contact and contact.status=='pending' else
                        name+'这次明确拒绝了发送。' if contact and contact.status=='refuse' else
                        '请求已经私信送达'+name+'，但目前还没有她给你发送消息的记录，不能认定她拒绝了。')
                    result['detail']={**result['detail'],'delivery_report':report}
                    if destination=='group':result['detail']['delivery_report']=action_contract.result_text(name,destination,contact.status if contact else None)
                return result
    system=(f'你只扮演{actors.ROLES[actor]}。用户可能请求你向另一个聊天窗口发消息，你根据性格、关系和保密态度自主决定。'
        '仅当当前用户明确要求跨会话发送才可send/later；用户只询问能力、引用别人说的话或提到群聊应ask/none。'
        '拒绝用refuse，需要明确发送内容用ask。不能为了迎合用户强行接受。'
        '只能转述当前用户授权公开的最少内容；不透露其他私密经历。可以拒绝，不能虚构已执行。'
        '必须结合本窗口前文理解指代和日常发送内容。“发到群里去”接着前面说饭好了，或“你叫他们吃饭呀”，就是发简短吃饭通知，不需要用户逐字给原文。'
        '用户说“怎么不发群里”“没看见你发”“再发一下”且前文已有发送任务，是检查或纠正实际发送；不要把它误判为泛泛询问能力。'
        '私人感情、秘密和未经授权的细节仍不得转发，吃饭等明确指定的普通通知只发必要一句。'
        '不扮演网络故障、转圈、信号差或假装点击发送；只有系统真正投递才知道成功，工具结果之前不能声称发了。'
        '输出JSON：decision(send/refuse/ask/later/none)、message(目标聊天台词)、reply(对用户的私聊答复)、'
        'reason、delay_minutes。send时reply不能声称已经发送，系统执行后再确认；later须有明确内容与延迟。'
        '目标为用户私聊时，只把message发到你自己与用户的私聊，不是在群里说一句代替私信。'
        'origin为群聊时，reply将公开给群里所有人，不披露私聊秘密或私人关系内容。')
    system+=('目标是另一角色时，你能真实私信对方，不必走到她面前，也不是替她操作手机。'
        '“让小柳给我发消息/让她发个1”是把请求传给小柳，由她决定是否给用户发；不能误解成让你冒充小柳发送。'
        '此时message要明确“用户让你给用户发消息/发个1”，不要把给用户写成给发送人自己。'
        '若用户要求收件角色在群里发言，recipient_task写{"type":"contact_group","message_hint":"群聊内容"}，message明确转达发到群聊。输出额外字段recipient_task：若用户要求收件角色联系用户，写{"type":"contact_user","message_hint":"1或最少必要内容"}；否则null。'
        '没有真实通信结果不能说已当面转告、已经喊到、对方不想发；这些不能靠动作叙述虚构。')
    system+=('同时输出sent_reply，表示“系统确认已成功转达/发送后”你会对用户说的简短自然一句话。'
        '按你的性格表达，可调侃或干脆，不套客服话，不说“请求已送达”“由她自行决定”，不附括号状态。'
        '目标是另一角色时，sent_reply只能确认你转达过，不能提前声称她已执行。')
    decision=await ask_json(cfg,system,{'target':'群聊' if target=='group' else '你与用户的私聊' if target=='user' else actors.ROLES[target],
        'origin':origin,'persona':persona,'relationship':relationship,
        'confidentiality':behavior.confidentiality,'recent':[{'role':m['role'],'content':m['content']} for m in messages[-12:]],
        'current_user_request':next((m['content'] for m in reversed(messages) if m['role']=='user'),'')})
    task=decision.get('recipient_task')
    if target in actors.ROLES and decision.get('decision') in ('send','later'):
        if re.search(r'群里|群聊|群中|群内',current) and re.search(r'发|说',current):
            task={'type':'contact_group','message_hint':current[-400:]}
    if target in actors.ROLES and isinstance(task,dict) and task.get('type') in ('contact_user','contact_group'):
        # This metadata comes only from a current human-authorized requested action,
        # never from an autonomous peer's claims about what the user supposedly said.
        decision['_recipient_task']=task
    action=await asyncio.to_thread(insert_action,user,world,actor,target,'requested',source,decision)
    def link(data):
        for record in data.get('recent_log',[])+data.get('memory_inbox',[]):
            if record.get('id')==source:
                record.setdefault('action_id',action['id'])
                record['action_ids']=list(dict.fromkeys(record.get('action_ids',[])+[action['id']]))
    await asyncio.to_thread(storage.get_store().mutate,*storage.scope.get(),link)
    from backend.app.memory import memory_autosave
    memory_autosave.link(*storage.scope.get(),source,action['id'])
    return action


def reserve(identity,now):
    with Session(storage.get_store().engine) as db:
        result=db.execute(update(Action).where(Action.id==identity,Action.due_at<=now,Action.attempts<3,
            or_(Action.status=='pending',(Action.status=='sending') & (Action.lease_until<now)))
            .values(status='sending',lease_until=now+180,attempts=Action.attempts+1))
        db.commit()
        return result.rowcount==1


def finish(identity,status,extra=None):
    with Session(storage.get_store().engine) as db:
        row=db.get(Action,identity)
        if row:
            row.status=status;row.lease_until=0
            if extra:
                row.detail={**row.detail,**extra}
            db.commit()


async def deliver(identity):
    """Idempotent message IDs survive process crashes between send and action commit."""
    from backend.app.runtime import initiative_control
    from backend.app.core import services
    started=time.monotonic()
    with Session(storage.get_store().engine) as db:
        pending=db.get(Action,identity)
        if pending and pending.kind in ('autonomous','reaction','inquiry') and not initiative_control.enabled(pending.user_id,pending.world_id):
            return action_data(pending)
    if not await asyncio.to_thread(reserve,identity,time.time()):
        with Session(storage.get_store().engine) as db:
            row=db.get(Action,identity)
            return action_data(row) if row else {'id':identity,'status':'cancelled','detail':{}}
    with Session(storage.get_store().engine) as db:
        row=db.get(Action,identity)
        user,world,actor,target,detail,kind=row.user_id,row.world_id,row.actor,row.target,row.detail,row.kind
        created=row.created_at
        attempt=row.attempts
    channel=actor if target=='user' else target
    if created < storage.get_store().read(user,world).get('memory_reset_at',0):
        finish(identity,'cancelled')
        return {'id':identity,'status':'cancelled','detail':detail}
    try:
        from backend.app.conversations import message_service
        from backend.app.conversations import peer_chat
        transport=await asyncio.to_thread(message_service.deliver,user,world,actor,target,detail['message'],
            peer_chat.key('action',identity) if target in actors.ROLES else identity,identity)
        if transport.get('status')=='cancelled':
            return {'id':identity,'status':'cancelled','detail':{**detail,'suppressed_reason':'repeated_initiative'}}
        mid=transport['id'];inserted=transport['inserted']
        if kind=='autonomous' and target in ('user','group') and detail.get('image_prompt'):
            from backend.app.characters import image_tools
            from backend.app.core import services
            with window(user,world,channel):
                try:
                    calls={0:{'name':'generate_image','arguments':json.dumps(
                        {'prompt':detail['image_prompt'],'actor':actor},ensure_ascii=False)}}
                    job=await asyncio.to_thread(image_tools.enqueue,'action-'+identity,calls)
                    image_tools.start(job)
                    with Session(storage.get_store().engine) as db:
                        action=db.get(Action,identity)
                        action.detail={**action.detail,'image_job_id':job['id']};db.commit()
                except Exception as error:
                    from backend.app.infrastructure import task_health
                    task_health.record(user,world,identity,'initiative_image',error,attempt,0)

        if target in actors.ROLES:
            return {'id':identity,'status':'sent','target':target,'detail':{**detail,'message_id':mid}}
        with window(user,world,channel):
            from backend.app.world import world_events
            record=next((m for m in chat_history.messages(50) if m['id']==mid),None)
            if record is None:raise RuntimeError('Delivered message unavailable')
            try:
                await asyncio.to_thread(world_events.capture,user,world,channel,record,detail.get('physical_event'))
                await asyncio.to_thread(world_events.delivery,user,world,actor,target,mid,detail['message'],record['created_at'])
                from backend.app.infrastructure import maintenance
                await asyncio.to_thread(maintenance.enqueue,user,world,channel,mid,record['created_at'],True)
                services.generation().wake_maintenance()
            except Exception as error:
                from backend.app.infrastructure import task_health
                task_health.record(user,world,identity,'action_event',error,attempt,int((time.monotonic()-started)*1000))
            if inserted:
                from backend.app.conversations import push_notifications
                try:
                    await asyncio.to_thread(push_notifications.enqueue,user,world,channel,mid)
                except Exception as error:
                    from backend.app.infrastructure import task_health
                    task_health.record(user,world,identity,'notification',error,attempt,int((time.monotonic()-started)*1000))
        return {'id':identity,'status':'sent','detail':{**detail,'message_id':mid}}
    except RuntimeError:
        # A busy conversation is retried later, not treated as an accepted send.
        with Session(storage.get_store().engine) as db:
            row=db.get(Action,identity)
            row.status='pending' if row.attempts<3 else 'failed'
            status=row.status
            row.due_at=time.time()+60;row.lease_until=0
            db.commit()
        return {'id':identity,'status':status,'detail':detail}
    except Exception as error:
        from backend.app.infrastructure import task_health
        task_health.record(user,world,identity,'action',error,attempt,int((time.monotonic()-started)*1000))
        await asyncio.to_thread(finish,identity,'failed',{'error':'发送失败，未确认送达'})
        return {'id':identity,'status':'failed','detail':detail}


def quiet(behavior,now):
    return False


def eligible(user,world,actor,behavior,now):
    return True


async def consider_reaction(identity):
    """At most one reply to a role-originated message; never recurse on reactions."""
    from backend.app.core import context as execution_context, services
    with Session(storage.get_store().engine) as db:
        row=db.get(Action,identity)
        if not row or row.status!='sent' or row.kind=='reaction' or row.target!='group':
            return
        if row.detail.get('reaction_checked'):
            return
        if not claim_check('reaction-'+identity[:48],time.time(),31536000):
            return
        user,world,sender,target,content=row.user_id,row.world_id,row.actor,row.target,row.detail.get('message','')
        from backend.app.runtime import initiative_control
        if not initiative_control.enabled(user,world):return
        # This marker is committed before model calls; failures cannot create reply storms.
        updated=db.execute(update(Action).where(Action.id==identity)
            .values(detail={**row.detail,'reaction_checked':True}))
        db.commit()
        if not updated.rowcount:
            return
    if target=='group':
        scene=storage.get_store().read(user,world).get('scene_state',{})
        possible=list(actors.ROLES)
        possible=[role for role in possible if role!=sender]
        channel='group'
    else:
        possible=[target];channel=target
    for recipient in possible:
        if not initiative_control.enabled(user,world):return
        with window(user,world,recipient):
            behavior=settings()
            if not behavior.respond_to_roles or quiet(behavior,time.time()):
                continue
            cfg=services.models().load_config()
            if services.models().check_cfg(cfg):
                continue
            persona=storage.read_document('persona','')
        with window(user,world,channel):
            chat_history.bootstrap()
            context=chat_history.messages(8)
            if channel!='group':
                context=[message for message in context if message['speaker']==actors.ROLES[sender]]
            if channel!='group':
                context=[message for message in context if message['speaker']==actors.ROLES[sender]]
        decision=await ask_json(cfg,
            f'你是{actors.ROLES[recipient]}。另一角色刚发来消息。根据性格和话题决定是否回应，可不回答；不要替别人说话。'
            '只使用下面的公开或收件窗口内容，不编造传闻或私聊秘密。只输出JSON：decision(send/none)、message、reason。',
            {'persona':persona,'sender':actors.ROLES[sender],'message':content,'context':context})
        if decision.get('decision') not in ('send','none'):
            return
        reply_target='group' if target=='group' else sender
        reaction=await asyncio.to_thread(insert_action,user,world,recipient,reply_target,'reaction',identity,decision)
        if reaction['status']=='pending':
            await deliver(reaction['id'])
        # Each recipient can independently reply; reactions do not recursively trigger reactions.


def claim_check(identity,now,interval=1800):
    """Atomically reserve a scheduling checkpoint across workers."""
    with Session(storage.get_store().engine) as db:
        row=db.get(Checkpoint,identity)
        if row:
            result=db.execute(update(Checkpoint).where(Checkpoint.id==identity,Checkpoint.next_check<=now).values(next_check=now+interval))
            db.commit();return bool(result.rowcount)
        try:
            db.add(Checkpoint(id=identity,next_check=now+interval));db.commit();return True
        except IntegrityError:
            db.rollback();return False


async def autonomous(user,world,actor,now=None):
    from backend.app.runtime import autonomy
    return await autonomy.consider(user,world,actor,now)


async def tick():
    """Periodic workers can restart safely; persisted intentions remain in the DB."""
    now=time.time()
    if not await asyncio.to_thread(claim_check,'global-role-tick',now,45):
        return
    with Session(storage.get_store().engine) as db:
        db.execute(update(Action).where(Action.status=='sending',Action.lease_until<now,Action.attempts>=3).values(status='failed',lease_until=0))
        db.commit()
        pending=list(db.scalars(select(Action.id).where(Action.due_at<=now,Action.attempts<3,
            or_(Action.status=='pending',(Action.status=='sending') & (Action.lease_until<now))).order_by(Action.due_at).limit(10)))
        candidates=[(row.user_id,row.data.get('world_id'),row.data.get('channel'))
            for row in db.scalars(select(storage.Conversation)) if row.data.get('world_id') and row.data.get('channel') in actors.ROLES]
    for identity in pending:
        await deliver(identity)
    from backend.app.conversations import peer_chat
    await asyncio.to_thread(peer_chat.run_tick)
    for user,world,actor in candidates:
        try:
            await autonomous(user,world,actor)
        except Exception:
            # API outages should not stop other roles or expose credentials to logs.
            continue
    with Session(storage.get_store().engine) as db:
        parents=[row.id for row in db.scalars(select(Action).where(Action.status=='sent',
            Action.kind.in_(['requested','autonomous']),Action.created_at>now-86400).order_by(Action.created_at.desc()).limit(20))
            if not row.detail.get('reaction_checked') and row.target!='user'][:3]
    for identity in parents:
        try:
            await consider_reaction(identity)
        except Exception:
            continue
    from backend.app.conversations import push_notifications
    await asyncio.to_thread(push_notifications.flush)


def cancel(user,world,identity):
    with Session(storage.get_store().engine) as db:
        result=db.execute(update(Action).where(Action.id==identity,Action.user_id==user,Action.world_id==world,
            Action.target.in_(['user','group']),Action.status=='pending').values(status='cancelled'))
        db.commit();return bool(result.rowcount)
