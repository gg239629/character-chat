"""Wall-clock advancement without model calls or fabricated conversations."""
import time
from datetime import datetime, timezone, timedelta
from backend.app.characters import actors
from backend.app.world import world_time


def fresh_scene(now=None,offset=480):
    now=time.time() if now is None else now
    hour=datetime.fromtimestamp(now,timezone(timedelta(minutes=offset))).hour
    seed='合租屋·卧室休息' if hour<7 or hour>=23 else '合租屋·自由活动'
    data={'timezone_offset_minutes':offset,'scene_state':{
        '地点':'合租屋','在场人物':'各自房间','主要交谈角色':'未知','上一段停在':'从当前现实时间重新开始',
        **{name:seed for name in actors.ROLES.values()}},
        'role_states':{role:{'status':seed,'source':'reset','evidence_at':now} for role in actors.ROLES}}
    advance(data,now)
    return data


def advance(data, now=None):
    now=time.time() if now is None else now
    offset=max(-720,min(840,int(data.get('timezone_offset_minutes',480))))
    current=datetime.fromtimestamp(now,timezone(timedelta(minutes=offset)))
    stamp=current.strftime('%Y-%m-%d %H:%M')
    previous=data.get('realtime_clock',{})
    if previous.get('minute')==stamp:return False
    origin=previous.get('origin_date',current.date().isoformat())
    try:day=max(1,(current.date()-datetime.fromisoformat(origin).date()).days+1)
    except ValueError:origin=current.date().isoformat();day=1
    data['realtime_clock']={'minute':stamp,'origin_date':origin,'checked_at':now}
    data['story_clock']={'day':day,'minute':current.hour*60+current.minute}
    scene=data.setdefault('scene_state',{});scene['时间']=stamp
    for role,name in actors.ROLES.items():
        state=data.setdefault('role_states',{}).setdefault(role,{})
        status=state.get('status') or scene.get(name,'未知')
        if status=='未知':continue
        if state.get('source')!='elapsed_activity':
            state.setdefault('last_confirmed_status',status)
            if state.get('last_confirmed_status')!=status:
                state['last_confirmed_status']=status
                state['last_confirmed_at']=state.get('evidence_at',now)
            state.setdefault('last_confirmed_at',state.get('evidence_at',now))
            state.setdefault('validity','confirmed')
        action=state.get('timed_action')
        if not action or action.get('status')!=status:
            # Observed activity begins when first seen. Unknown durations are bounded,
            # never interpreted as proof of arriving, eating, or meeting another person.
            minutes=480 if '睡觉' in status else 60 if any(x in status for x in ('开车','做饭','煮','吃饭','洗澡')) else 120
            evidence=state.get('evidence_at',now) if action is None else now
            started=evidence if isinstance(evidence,(float,int)) and 0<evidence<=now else now
            action={'status':status,'started_at':started,'ends_at':started+minutes*60}
            state['timed_action']=action
        if now>=action['ends_at']:
            location=status.split('·',1)[0] if '·' in status else '原处'
            status=location+'·暂无新动态'
            state['status']=status;state['source']='elapsed_activity'
            state['validity']='stale'
            state['timed_action']={'status':status,'started_at':now,'ends_at':now+86400}
        state.setdefault('status',status)
        # Compatibility display stays concise; evidence and uncertainty remain
        # separate so the model cannot mistake timeout for a completed action.
        if state.get('validity')=='stale':scene[name]=state['status']
    return True


def sync(user,world,data=None):
    from backend.app.infrastructure import storage
    store=storage.get_store()
    if data is None:data=store.read(user,world)
    now=time.time()
    if now-data.get('realtime_clock',{}).get('checked_at',0)<60:return data
    from backend.app.world import state_events
    return state_events.sync_time(user,world,now)
