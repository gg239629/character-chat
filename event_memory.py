"""Evidence-backed actions with immutable initial witnesses, not shared omniscience."""
import re
import hashlib
import storage
import actors


def capture(user,world,channel,record,audience=None):
    from continuity import is_plan
    if record.get('role')!='assistant':return
    actor=next((r for r,n in actors.ROLES.items() if n==record.get('speaker')),None)
    if not actor:return
    actions=[t.strip() for t in re.findall(r'[（(]([^（）()]*)[）)]',record['content']) if not is_plan(t)
        if re.search(r'走进|离开|回到|拿起|放下|收起|打开|关上|吃完|洗完|递给|取出|开始|坐下|起床|出门|回家',t)
        and not re.search(r'假装|装作|准备|打算|如果|假如|计划|想要|没有|并未|明天|昨天|告诉|转告|发消息',t)]
    readers=list(actors.ROLES) if channel=='group' else [r for r in (audience or []) if r in actors.ROLES]
    # A factual statement in an actual message is something recipients heard,
    # not an observed event. It must never overwrite witnessed world evidence.
    report=bool(readers and not actions and re.search(r'快递|包裹|礼物|钥匙|出门|回家|到家|离开|收到|拿到|放在',record['content'])
        and not re.search(r'[?？]|如果|假如|准备|打算|明天|昨天',record['content']))
    if not actions and not report:return
    def update(data):
        events=data.setdefault('witnessed_events',[])
        if report:
            identity=hashlib.sha256((record['id']+':report').encode()).hexdigest()
            if not any(e['id']==identity for e in events):
                events.append({'id':identity,'actor':actor,'text':record['content'][:500],'source_id':record['id'],
                    'created_at':record['created_at'],'story_time':data.get('story_clock',{}),
                    'witnesses':[],'participants':[actor],'heard_by':readers,
                    'communication_source':record['id'],'channel':channel,'kind':'reported_action'})
        for i,action in enumerate(actions):
            identity=hashlib.sha256(f"{record['id']}:{i}".encode()).hexdigest()
            if any(e['id']==identity for e in events):continue
            # Explicit recipients of a physical handover can observe the action;
            # being in the same home alone never establishes line of sight.
            witnesses=[actor]
            for role,name in actors.ROLES.items():
                if role!=actor and re.search(r'(?:递给|交给)'+name+r'|放到'+name+r'手里|在'+name+r'面前展示',action):
                    witnesses.append(role)
            own=data.get('role_states',{}).get(actor,{})
            location=own.get('status',data.get('scene_state',{}).get(actors.ROLES[actor],'未知')).split('·')[0]
            events.append({'id':identity,'actor':actor,'text':action[:500],'source_id':record['id'],
                'created_at':record['created_at'],'story_time':data.get('story_clock',{}),
                'witnesses':witnesses,'participants':witnesses,'location':location,
                'heard_by':[r for r in readers if r not in witnesses],
                'communication_source':record['id'] if readers else None,'channel':channel,'kind':'observed_action'})
        data['witnessed_events']=events[-200:]
    storage.get_store().mutate(user,world,update)


def recall(user,world,actor,query):
    import long_memory
    events=[e for e in storage.get_store().read(user,world).get('witnessed_events',[])
        if actor in e['witnesses'] or actor in e.get('heard_by',[])]
    ranked=long_memory.rank(query,events)
    indices=list(range(max(0,len(events)-2),len(events)))
    indices += [i for score,_,i in ranked if score>=.2 and i not in indices][:3]
    return [{'kind':'observed_action' if actor in e['witnesses'] else 'reported_action',
        'basis':'亲历' if actor in e['witnesses'] else '通过实际消息得知，不能当作本人目击',
        'reported_by':actors.ROLES[e['actor']],'text':e['text'],'time':e['story_time'],
        'location':e.get('location','未知'),'source_id':e['source_id']} for e in (events[i] for i in sorted(indices))]
