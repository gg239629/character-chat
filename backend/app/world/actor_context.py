"""Small actor-visible facts, relevant evidence and real delivery results."""
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.infrastructure import storage
from backend.app.world import state_events,continuity,world_events,world_time


def build(user,world,actor,query,messages=()):
    state_events.adopt_legacy(user,world)
    data=storage.get_store().read(user,world)
    state=data.get('role_states',{}).get(actor,{})
    known=state.get('last_confirmed_status',state.get('status',data.get('scene_state',{}).get(state_events.actors.ROLES[actor],'未知')))
    facts={'time':world_time.display(world_time.clock(data)),
        'self':{'last_confirmed_status':known,'confirmed_at':state.get('last_confirmed_at',state.get('evidence_at')),
                'validity':state.get('validity','stale' if state.get('source')=='elapsed_activity' else 'confirmed'),
                'source_id':state.get('source_id')},
        **continuity.context(user,world,actor)}
    # A character remembers its own physical actions across windows, without
    # loading anyone else's private transcript or treating old dialogue as current.
    with Session(storage.get_store().engine) as db:
        own=db.scalars(select(state_events.Event).where(state_events.Event.user_id==user,
            state_events.Event.world_id==world,state_events.Event.actor==actor,
            state_events.Event.kind.in_(['action','state']),
            state_events.Event.occurred_at>data.get('memory_reset_at',0))
            .order_by(state_events.Event.occurred_at.desc()).limit(4))
        facts['self']['recent_actions']=[{'action':r.detail.get('text','')[:300],
            'time':r.occurred_at} for r in own]
    physical=world_events.knowledge(user,world,actor)
    if physical:facts['known_world_facts']=physical
    recall=bool(re.search(r'记得|以前|之前|上次|刚才|昨天|约定|答应|包裹|快递|礼物|钥匙|哪里|在哪|做了|干了|怎么|为什么',query))
    events=state_events.history(user,world,actor,query,limit=5 if recall else 2,
        exclude_sources=[m['id'] for m in messages if m.get('id')]) if recall else []
    # Strip transport IDs from optional historical excerpts. The ledger retains
    # full provenance; the model only needs time, actor, evidence and basis.
    history=[{k:v for k,v in e.items() if k in ('actor','kind','created_at','text','basis')} for e in events
        if not any(e['text'] and e['text'] in m.get('content','') for m in messages)]
    communication=[]
    if re.search(r'发|消息|群|私聊|私信|联系|转告|通知|收到|回复|告诉',query):
        with Session(storage.get_store().engine) as db:
            rows=db.scalars(select(state_events.Event).where(state_events.Event.user_id==user,
                state_events.Event.world_id==world,state_events.Event.kind=='delivery',
                state_events.Event.audience.contains('|'+actor+'|')).order_by(state_events.Event.occurred_at.desc()).limit(6))
            communication=[{'sender':r.actor,'target':r.detail['target'],'status':'sent',
                'message_id':r.detail['message_id'],'time':r.occurred_at} for r in rows]
        # Existing action rows are authoritative and cover deliveries predating
        # the ledger. Do not infer read/replied status from a sent receipt.
        from backend.app.runtime import role_actions
        for a in role_actions.audit(user,world,actor,internal=True):
            mid=a.get('detail',{}).get('message_id')
            if a['status']=='sent' and mid and not any(r['message_id']==mid for r in communication):
                communication.append({'sender':actor,'target':a['target'],'status':'sent','message_id':mid})
            if len(communication)>=6:break
    return facts,history,communication[:6]
