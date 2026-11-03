"""Durable evidence ledger and atomic projections for character state.

Raw evidence is immutable; current state is a projection, never a second history.
Visibility is assigned by the server, not by the extraction model.
"""
import copy
import hashlib
import json
import re
import time
from sqlalchemy import String, Float, JSON, Index, select, update, or_
from sqlalchemy.orm import Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from backend.app.infrastructure import storage
from backend.app.characters import actors


class Event(storage.Base):
    __tablename__ = 'chat_world_events'
    __table_args__ = (Index('ix_world_event_owner_clock','user_id','world_id','occurred_at'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    world_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    audience: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[float] = mapped_column(Float, index=True)
    recorded_at: Mapped[float] = mapped_column(Float)
    detail: Mapped[dict] = mapped_column(JSON)


def identity(user, world, source, actor, kind, key=''):
    return hashlib.sha256(json.dumps([user, world, source, actor, kind, key], ensure_ascii=False).encode()).hexdigest()


def readers(channel, actor, audience=None):
    if channel == 'group': return list(actors.ROLES)
    if channel.startswith('peer:'):
        return sorted({r for r in (audience or []) + [actor] if r in actors.ROLES})
    return [channel] if channel in actors.ROLES else [actor] if actor in actors.ROLES else []


def _project(data, event):
    """Apply newer evidence only. Plans/reports do not move physical state."""
    d=event['detail'];actor=event['actor'];stamp=event['occurred_at'];kind=event['kind']
    provenance={'source_id':event['source_id'], 'event_id':event['id'], 'updated_at':stamp}
    if kind=='physical_fact':
        from backend.app.world import world_events
        world_events._observe(data,actor,d['text'],event['source_id'],stamp,event['channel'],d['role'],d.get('candidate'))
        return
    if kind in ('action','report') and d.get('method')!='legacy':
        # Bounded compatibility cache for existing readers. The ledger is the
        # permanent history; this cache is never used as the source of truth.
        cache=data.setdefault('witnessed_events',[])
        cache.append({'id':event['id'],'ledger_id':event['id'],'actor':actor,
            'text':d.get('text',''),'source_id':event['source_id'],'created_at':stamp,
            'story_time':data.get('story_clock',{}),'channel':event['channel'],
            'witnesses':d.get('witnesses',[]),
            'heard_by':[r for r in d.get('known_to',[]) if r not in d.get('witnesses',[])],
            'kind':'observed_action' if kind=='action' else 'reported_action'})
        data['witnessed_events']=cache[-200:]
    if kind == 'profile':
        profile=data.setdefault('user_profile', {})
        for key,value in d['profile'].items():
            if stamp >= profile.get(key, {}).get('updated_at', 0):
                profile[key]={'value':value, **provenance}
        return
    if kind not in ('action', 'state'): return
    position=d.get('position');status=d.get('status')
    cutoff=max(data.get('role_states',{}).get(actor,{}).get('evidence_at',0),data.get('world_clock_revision',0))
    if stamp<cutoff:return
    old=data.get('role_states',{}).get(actor,{})
    if old.get('source_id')==event['source_id'] and old.get('source')=='explicit_transition' and d.get('method')=='model':return
    if position:
        positions=data.setdefault('role_positions', {})
        if stamp >= positions.get(actor, {}).get('updated_at', 0):
            positions[actor]={**position, **provenance, 'basis':'actual_action', 'known_to':d['known_to']}
    if actor not in actors.ROLES or not status: return
    states=data.setdefault('role_states', {});old=states.get(actor, {})
    cutoff=max(old.get('evidence_at', 0), data.get('world_clock_revision', 0))
    if stamp < cutoff: return
    # A positional observation and an action can share the same source. A later
    # model pass may refine the activity, but cannot undo a completed transition.
    states[actor]={**old, 'status':status, **provenance, 'source':d.get('method','rule'),
                   'evidence_at':stamp, 'validity':'confirmed', 'last_confirmed_status':status,
                   'last_confirmed_at':stamp, 'known_to':d['known_to']}
    states[actor].pop('timed_action', None)
    data.setdefault('scene_state', {})[actors.ROLES[actor]]=status


def commit(user, world, items, change=None):
    """Event insert and all current-state projections share one transaction.

    change(data) may return additional events for time-based transitions. It is
    rerun against the latest version if another writer wins the transaction.
    """
    if not items and change is None:return
    store=storage.get_store()
    for attempt in range(10):
        try:
            with Session(store.engine) as db:
                row=db.get(storage.Conversation, world)
                if not row or row.user_id != user:raise PermissionError('Story unavailable')
                data=copy.deepcopy(row.data)
                legacy=[]
                if not data.get('event_ledger_adopted'):
                    legacy=_legacy_items(user,world,data)
                    data['event_ledger_adopted']=True
                batch=legacy+list(items)+(change(data) or [] if change else [])
                new=[]
                for item in batch:
                    if item['occurred_at'] <= data.get('memory_reset_at', 0):continue
                    if db.get(Event,item['id']):continue
                    db.add(Event(user_id=user,world_id=world,recorded_at=time.time(),
                        audience='|'+'|'.join(item['detail'].get('known_to',[]))+'|',**item))
                    _project(data,item);new.append(item)
                if not new and data==row.data:return
                result=db.execute(update(storage.Conversation).where(storage.Conversation.id==world,
                    storage.Conversation.version==row.version).values(data=data,version=row.version+1))
                if not result.rowcount:db.rollback();continue
                db.commit()
                cached=storage.request_snapshot.get()
                if cached and cached['scope']==(user,world):cached['data']=data
                return
        except IntegrityError:
            if attempt==9:raise
    raise RuntimeError('Concurrent world event update; retry')


def make(user,world,record,channel,actor,kind,detail,key=''):
    return dict(id=identity(user,world,record['id'],actor,kind,key),actor=actor,kind=kind,
                source_id=record['id'],channel=channel,occurred_at=record['created_at'],detail=detail)


def _speaker(record):
    if record.get('role')=='user': return 'user'
    return next((r for r,n in actors.ROLES.items() if n==record.get('speaker') or
                 record.get('content','').startswith((n+'：',n+':'))),None)


def action_texts(record):
    """Narration may be extracted semantically; quoted dialogue is not an action."""
    from backend.app.world.continuity import is_plan
    text=record.get('content','')
    if text.startswith('[生图请求]') or re.search(r'\[\[image:[0-9a-f]{64}\]\]',text):return []
    chunks=re.findall(r'[（(]([^（）()]*)[）)]',text)
    return [s.strip() for s in chunks if s.strip() and not is_plan(s) and
            not re.search(r'撒谎|谎称|其实没|并没|没动|不曾|假装|开玩笑|[?？]|告诉|转告|发消息',s)]


def capture(user,world,channel,record,audience=None,candidate=None):
    """Deterministic immediate path; one source drives position, state and history."""
    from backend.app.world import continuity,scene_labels,world_events
    text=record.get('content','')
    if text.startswith('[生图请求]') or re.search(r'\[\[image:[0-9a-f]{64}\]\]',text):return
    actor=_speaker(record)
    if not actor:return
    known=readers(channel,actor,audience);text=record.get('content','')
    changes,position=continuity.extract(record,channel)
    items=[]
    if changes:items.append(make(user,world,record,channel,actor,'profile',{'profile':changes,'known_to':known}))
    chunks=action_texts(record)
    roles=[channel] if channel in actors.ROLES else list(actors.ROLES) if channel=='group' else [actor]
    transitions={r:scene_labels.physical_transition([record],actors.ROLES[r],channel==r)
                 for r in roles if r in actors.ROLES}
    for role,status in transitions.items():
        if status:
            pos={'place':status.split('·')[0],'relative_to':None,'relation':None}
            items.append(make(user,world,record,channel,role,'state',{'status':status,'position':pos,
                'text':text[:500],'known_to':known,'witnesses':[role],'method':'explicit_transition'}))
    # Each narrated action is retained, even when it contains no fixed verb from
    # the old keyword list. Position extraction remains a deterministic fast path.
    if chunks:
        for i,chunk in enumerate(chunks):
            witnesses=[actor]
            for role,name in actors.ROLES.items():
                if role!=actor and re.search(r'(?:递给|交给)'+name+r'|放到'+name+r'手里|在'+name+r'面前展示',chunk): witnesses.append(role)
            detail={'text':chunk[:500],'known_to':sorted(set(known+witnesses)),
                    'witnesses':witnesses,'method':'rule'}
            # Only the final action supplies the final position for this message.
            if i==len(chunks)-1 and position:
                detail['position']=position
                if actor in actors.ROLES and position.get('place') and (not transitions.get(actor) or position['place']!=transitions[actor].split('·')[0]):
                    detail['status']=scene_labels.label(position['place']+'·'+chunk,position['place']+'·活动')
            items.append(make(user,world,record,channel,actor,'action',detail,str(i)))
    elif position:
        items.append(make(user,world,record,channel,actor,'action',{'text':text[:500],
            'position':position,'known_to':known,'witnesses':[actor],'method':'rule'}))
    if not chunks and actor in actors.ROLES and known and re.search(r'快递|包裹|礼物|钥匙|出门|回家|到家|离开|收到|拿到|放在',text) and not re.search(r'[?？]|如果|假如|准备|打算|明天|昨天',text):
        items.append(make(user,world,record,channel,actor,'report',{'text':text[:500],
            'known_to':known,'witnesses':[],'method':'message'}))
    if candidate or world_events.meal_action(text) or (actor=='user' and re.search(r'在家|到家|回到家|在外|出门|上班|在厨房|在客厅|在餐厅|在阳台',text)):
        items.append(make(user,world,record,channel,actor,'physical_fact',{
            'text':text,'role':record['role'],'candidate':candidate,'known_to':known,'method':'rule'}))
    commit(user,world,items)


def candidates(messages,channel):
    """No verb gate: semantic extraction gets all actual narration plus direct
    user descriptions. Plain assistant claims still cannot move physical state.
    """
    result=[]
    for m in messages:
        text=m.get('content','')
        if text.startswith('[生图请求]') or re.search(r'\[\[image:[0-9a-f]{64}\]\]',text):continue
        if action_texts(m) or (m.get('role')=='user' and re.search(r'小婉|小研|小柳|我们|咱俩|咱们|我(?:现在|已经)?在|你(?:现在|已经|刚)?(?:在|到了|坐|站|走|回)',m.get('content',''))):
            result.append(m)
    return result


EXTRACTION_SYSTEM='''你是世界状态证据提取器。只提取本批原文中已经实际发生的动作/位置/状态变化。
角色助手的台词只是说法，只有括号内本人实际动作可更新物理状态。用户明确事实叙述可以作为证据。
计划、请求、假设、提问、引用、否定、谎言、未来和回忆不作为当前完成动作。用户不能替未提及的人移动。
不要推测动作时长、完成结果或别人位置，不生成通信成功记录，不修改时间。没有依据返回空events。
每个输出项必须引用输入source_id并逐字引用evidence；不输出知情人，权限由服务端决定。
只输出JSON：{"events":[{"source_id":"原文ID","actor":"wan/yan/liu/user",
"evidence":"原文连续片段","status":"厨房·洗杯子",
"position":{"place":"厨房或null","relative_to":"user/wan/yan/liu或null","relation":"旁边或null"}}]}。
没有位置变化时省略position，没有状态变化时省略status。'''


def apply_model(user,world,channel,records,answer):
    """Validate sources, authorship and evidence before applying model output."""
    from backend.app.world import continuity,scene_labels
    by_id={r['id']:r for r in records};items=[]
    proposed=answer.get('events',[]) if isinstance(answer,dict) else []
    if not isinstance(proposed,list): return
    for index,e in enumerate(proposed[:24]):
        if not isinstance(e,dict):continue
        record=by_id.get(e.get('source_id'));actor=e.get('actor');evidence=e.get('evidence')
        if not record or actor not in ('user',*actors.ROLES) or not isinstance(evidence,str) or not evidence.strip():continue
        source_actor=_speaker(record);text=record.get('content','')
        if evidence not in text or continuity.is_plan(evidence) or re.search(r'撒谎|谎称|没动|并没|[?？“”「」"]',evidence):continue
        if channel in actors.ROLES and actor not in (channel,'user'):continue
        if record.get('role')=='assistant':
            if source_actor!=actor or not any(evidence in s for s in action_texts(record)):continue
        elif record.get('role')=='user':
            if continuity.is_plan(text) or re.search(r'[?？“”「」"]|说过|告诉|请|吧',text):continue
            if actor!='user' and actors.ROLES[actor] not in text and not (actor==channel and re.search(r'我们|咱俩|咱们|你(?:现在|已经|刚)?(?:在|到了|坐|站|走|回)',text)):continue
        else:continue
        detail={'text':evidence[:500],'known_to':readers(channel,actor),'witnesses':[actor],
                'method':'model','evidence_offset':text.find(evidence)}
        status=e.get('status')
        if actor in actors.ROLES and isinstance(status,str) and status.strip() and len(status)<=24 and ('·' in status or status in evidence):
            detail['status']=status
        pos=e.get('position')
        if isinstance(pos,dict):
            place=pos.get('place');target=pos.get('relative_to');relation=pos.get('relation')
            if place is not None and (not isinstance(place,str) or not 1<=len(place)<=20 or place not in evidence):continue
            if target is not None and target not in ('user',*actors.ROLES):continue
            if target==actor or (target is not None and (not isinstance(relation,str) or relation not in evidence)):continue
            if target is not None:
                named=actors.ROLES.get(target,'用户') in evidence
                addressed=bool('你' in evidence and (target=='user' if record.get('role')=='assistant' else target==channel))
                if not named and not addressed:continue
            if place or target:detail['position']={'place':place,'relative_to':target,'relation':relation if target else None}
        if not detail.get('status') and not detail.get('position'):continue
        # The model must not introduce a location absent from the evidence.
        if detail.get('status') and '·' in detail['status']:
            place=detail['status'].split('·')[0]
            data=storage.get_store().read(user,world)
            old=data.get('role_states',{}).get(actor,{}).get('status',data.get('scene_state',{}).get(actors.ROLES.get(actor,''),''))
            if place not in evidence and place!=old.split('·')[0]:continue
            if detail.get('position',{}).get('place') and detail['position']['place']!=place:continue
            if place in evidence and not detail.get('position'):
                detail['position']={'place':place,'relative_to':None,'relation':None}
        key=hashlib.sha256(evidence.encode()).hexdigest()
        items.append(make(user,world,record,channel,actor,'state',detail,'model-'+key))
    # Models may enumerate events out of order. The source text, rather than
    # response-array order, determines which action is the final observation.
    items.sort(key=lambda item:(item['occurred_at'],item['detail']['evidence_offset']))
    commit(user,world,items)


def history(user,world,actor,query='',limit=5,exclude_sources=()):
    """Query actor-visible history, scoped in SQL before loading content."""
    storage.get_store().read(user,world)
    from backend.app.memory import long_memory
    with Session(storage.get_store().engine) as db:
        base=select(Event).where(Event.user_id==user,Event.world_id==world,
            Event.audience.contains('|'+actor+'|'),Event.source_id.not_in(list(exclude_sources)),
            Event.kind.in_(['action','state','report','legacy_progress','legacy_state']))
        rows=list(db.scalars(base.order_by(Event.occurred_at.desc(),Event.id).limit(12)))
        # Search terms are bound parameters. Old events remain searchable without
        # loading the full ledger or including unrelated history in the prompt.
        terms=list(dict.fromkeys(re.findall(r'[\u4e00-\u9fff]{2}|[A-Za-z0-9_]{3,}',query)))[:24]
        if terms:
            rows+=list(db.scalars(base.where(or_(*[Event.detail['text'].as_string().contains(t,autoescape=True) for t in terms]))
                .order_by(Event.occurred_at.desc()).limit(100)))
        recent=[];candidates=[]
        seen=set()
        for row in rows:
            if row.id in seen:continue
            seen.add(row.id)
            d=row.detail
            if actor not in d.get('known_to',[]) or row.source_id in exclude_sources:continue
            item={'id':row.id,'source_id':row.source_id,'actor':row.actor,'kind':row.kind,
                  'created_at':row.occurred_at,'text':d.get('text',d.get('status','')),
                  'status':d.get('status'),'position':d.get('position'),
                  'basis':'亲历' if actor in d.get('witnesses',[]) else '通过实际消息得知，不能当作本人目击'}
            if len(recent)<2 and not any(x['source_id']==item['source_id'] and x['actor']==item['actor'] for x in recent):recent.append(item)
            candidates.append(item)
        ranked=long_memory.rank(query,candidates) if query else []
        selected=recent[:min(2,limit)]
        for score,_,index in ranked:
            if score<.2:continue
            item=candidates[index]
            if len(selected)>=limit:break
            if not any(x['source_id']==item['source_id'] and x['actor']==item['actor'] for x in selected):selected.append(item)
        return sorted(selected,key=lambda x:x['created_at'])


def delivery(user,world,actor,target,message_id,stamp):
    record={'id':message_id,'created_at':stamp}
    channel='group' if target=='group' else 'peer:'+ '-'.join(sorted([actor,target])) if target in actors.ROLES else actor
    commit(user,world,[make(user,world,record,channel,actor,'delivery',{
        'target':target,'message_id':message_id,'status':'sent','text':'消息已投递；不证明对方阅读或回复',
        'known_to':readers(channel,actor,[target] if target in actors.ROLES else None)})])


def delivery_in_transaction(db,user,world,actor,target,message_id,stamp):
    """Real message and sent receipt commit or roll back together."""
    owner=db.get(storage.Conversation,world)
    if not owner or owner.user_id!=user:raise PermissionError('Story unavailable')
    channel='group' if target=='group' else 'peer:'+'-'.join(sorted([actor,target])) if target in actors.ROLES else actor
    item=make(user,world,{'id':message_id,'created_at':stamp},channel,actor,'delivery',{
        'target':target,'message_id':message_id,'status':'sent',
        'text':'消息已投递；不证明对方阅读或回复',
        'known_to':readers(channel,actor,[target] if target in actors.ROLES else None)})
    if not db.get(Event,item['id']):
        db.add(Event(user_id=user,world_id=world,recorded_at=time.time(),
            audience='|'+'|'.join(item['detail']['known_to'])+'|',**item))


def sync_time(user,world,now):
    from backend.app.world import realtime_world
    def change(data):
        before=copy.deepcopy(data.get('role_states',{}));items=[]
        realtime_world.advance(data,now)
        for actor,state in data.get('role_states',{}).items():
            if state.get('validity')=='stale' and before.get(actor,{}).get('validity')!='stale':
                record={'id':'expired-'+str(state.get('last_confirmed_at',now)),'created_at':now}
                items.append(make(user,world,record,actor,actor,'freshness',{
                    'text':'最后确认的活动已超过估计窗口，当前活动未知；不代表已完成',
                    'last_confirmed_status':state.get('last_confirmed_status'),
                    'last_confirmed_at':state.get('last_confirmed_at'),'known_to':[actor],
                    'validity':'stale','method':'clock'}))
        return items
    commit(user,world,[],change)
    return storage.get_store().read(user,world)


def advance_story(user,world,candidate,active_roles,revision):
    """Compatibility story-clock progression retains every generated life event."""
    from backend.app.world import world_time
    def change(data):
        previous=copy.deepcopy(data.get('personal_progress',{}))
        if not world_time.apply(data,candidate,active_roles,revision):return []
        items=[]
        for actor,progress in data.get('personal_progress',{}).items():
            for p in progress:
                if p in previous.get(actor,[]):continue
                key=hashlib.sha256(json.dumps(p,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                record={'id':'routine-'+key,'created_at':revision if revision is not None else time.time()}
                items.append(make(user,world,record,actor,actor,'legacy_progress',{
                    'text':p.get('text',p.get('status','')),'known_to':[actor],'witnesses':[actor],
                    'story_time':p.get('story_time'),'method':'routine'}))
        return items
    commit(user,world,[],change)


def _legacy_items(user,world,data):
    """Archive the available pre-ledger evidence without reapplying old state."""
    items=[]
    for e in data.get('witnessed_events',[]):
        if e.get('ledger_id'):continue
        actor=e.get('actor')
        if actor not in actors.ROLES:continue
        record={'id':e.get('source_id',e['id']),'created_at':e.get('created_at',0)}
        known=sorted(set(e.get('witnesses',[])+e.get('heard_by',[])))
        items.append(make(user,world,record,e.get('channel','group'),actor,'report',
            {'text':e.get('text',''),'known_to':known,'witnesses':e.get('witnesses',[]),'method':'legacy'},e['id']))
    for actor,progress in data.get('personal_progress',{}).items():
        if actor not in actors.ROLES:continue
        for i,p in enumerate(progress):
            key=hashlib.sha256(json.dumps(p,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            record={'id':'legacy-progress-'+key,'created_at':data.get('world_clock_revision',0) or time.time()}
            items.append(make(user,world,record,actor,actor,'legacy_progress',{
                'text':p.get('text',p.get('status','')),'known_to':[actor],'witnesses':[actor],
                'story_time':p.get('story_time'),'method':'legacy_routine'},str(i)))
    for actor,state in data.get('role_states',{}).items():
        if actor not in actors.ROLES:continue
        status=state.get('last_confirmed_status',state.get('status'))
        if not status:continue
        record={'id':state.get('source_id') or 'legacy-state-'+actor,
                'created_at':state.get('last_confirmed_at',state.get('evidence_at',0)) or time.time()}
        items.append(make(user,world,record,actor,actor,'legacy_state',{
            'text':status,'status':status,'position':data.get('role_positions',{}).get(actor),
            'known_to':[actor],'witnesses':[actor],'method':'legacy_snapshot'}))
    return items


def adopt_legacy(user,world):
    """Idempotently preserve available old history, never invent lost rows."""
    data=storage.get_store().read(user,world)
    if not data.get('event_ledger_adopted'):commit(user,world,[],lambda d:[])
