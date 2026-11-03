"""Small witnessed physical facts and real delivery receipts, never shared private chat."""
import hashlib
import re
import time
from backend.app.characters import actors
from backend.app.infrastructure import storage

HOME_ROOMS=('厨房','客厅','卧室','阳台','餐厅','家里','在家')
UNCERTAIN=r'假装|装作|撒谎|谎称|骗|如果|假如|打算|准备|计划|明天|昨天'


def meal_action(text):
    """Extract only mundane cooking facts corroborated by actual action narration."""
    narratives=re.findall(r'[（(]([^（）()]*)[）)]',text)
    actions=' '.join(n for n in narratives if not re.search(UNCERTAIN+r'|没有|并未|没动',n))
    if not actions:return None
    food=next((name for name in ('面条','面','粥','饭','菜','汤') if name in text),'饭')
    ready=bool(re.search(r'关[了掉]?火|熄火|出锅|盛[好出]|端[出上]|掀开锅盖|煮好|做好',actions)
               and re.search(r'(?:面条|面|饭|粥|菜|汤).{0,5}(?:好了|熟了|做好|煮好)|(?:煮好|做好|盛好).{0,3}(?:面|饭|粥|菜|汤)',text))
    if ready:return {'food':food,'state':'ready','text':food+'已经做好，可以吃了'}
    if re.search(r'煮|下锅|烧水|开火',actions):return {'food':food,'state':'cooking','text':'正在做'+food}
    if re.search(r'吃完|收拾碗|洗碗',actions):return {'food':food,'state':'finished','text':food+'已经吃完'}
    return None


def _site(data,role):
    explicit=data.get('physical_presence',{}).get(role,{})
    if explicit:return explicit.get('site')
    scene=data.get('scene_state',{})
    status=data.get('role_states',{}).get(role,{}).get('status',scene.get(actors.ROLES[role],''))
    place=scene.get('地点','')
    # "Each at their own home" is not shared co-location.
    if '各自' not in place and any(room in place for room in HOME_ROOMS) and any(room in status for room in HOME_ROOMS):return 'story-home'
    return None


def _clock(data):
    from backend.app.world import world_time
    clock=world_time.clock(data)
    return clock['day']*1440+clock['minute']


def _observe(data,actor,text,source,stamp,channel,role='assistant',candidate=None):
    if text.startswith('[生图请求]') or re.search(r'\[\[image:[0-9a-f]{64}\]\]',text):return
    seen=data.setdefault('physical_event_sources',[])
    if source in seen:return
    seen.append(source);data['physical_event_sources']=seen[-120:]
    presence=data.setdefault('physical_presence',{})
    if role=='user' and not re.search(r'[“”「」]|如果|假如|昨天|明天',text):
        # A statement made in one private chat is not automatically known by everybody.
        known=list(actors.ROLES) if channel=='group' else [channel]
        away=re.search(r'我(?:现在|今天|还)?(?:不在家|在外面|在外边|出门了|去上班了)(?![吗么？?])',text)
        home=re.search(r'我(?:现在|已经)?(?:到家了|回到家了|在家呢)(?![吗么？?])',text)
        if away or home:presence['user']={'site':'away' if away else 'story-home','known_to':known,'source':source}
        for clause in re.split(r'[，,。！!？?\n]|而|但是',text):
            if re.search(r'不在|没在|吗|说|告诉|觉得|可能|是不是|怎么',clause):continue
            if re.search(r'(?:在家|在厨房|在客厅|在餐厅|在阳台)',clause):
                for key,name in actors.ROLES.items():
                    if name in clause:presence[key]={'site':'story-home','known_to':known,'source':source}
        return
    if not actor:return
    event=candidate or meal_action(text)
    if not event:return
    # These are tightly validated physical fields, not model-written summaries of private chat.
    if event.get('state') not in ('ready','cooking','finished') or event.get('food') not in ('面条','面','粥','饭','菜','汤'):return
    old=data.get('household_meal',{})
    clock=_clock(data)
    if stamp<old.get('created_at',0):return
    if old.get('state')=='ready' and event['state']=='cooking' and old.get('story_minute')==clock and not re.search(r'重新|又煮|另一锅|新煮',text):return
    site=_site(data,actor)
    observers=[key for key in actors.ROLES if key==actor or (site and _site(data,key)==site)]
    secret=bool(re.search(r'保密|别告诉|不要告诉|偷偷|秘密',text))
    if secret:observers=[actor]
    data['household_meal']={'id':hashlib.sha256(source.encode()).hexdigest()[:20],
        'food':event['food'],'state':event['state'],'cook':actor,'created_at':stamp,
        'story_minute':clock,'site':site,'observers':observers,'shareable':not secret,
        'source_id':source,'basis':'actual_action'}


def capture(user,world,channel,record,candidate=None):
    from backend.app.world import state_events
    state_events.capture(user,world,channel,record,candidate=candidate)


def ensure(user,world):
    """One-time replay of relevant evidence; no deletion or rewriting of old messages."""
    store=storage.get_store()
    if store.read(user,world).get('physical_events_initialized'):return
    from backend.app.runtime import role_actions
    from backend.app.conversations import chat_history
    rows=[]
    for channel in ['group',*actors.ROLES]:
        with role_actions.window(user,world,channel):
            rows.extend((record,channel) for record in chat_history.messages(30))
    rows.sort(key=lambda pair:pair[0]['created_at'])
    def replay(data):
        if data.get('physical_events_initialized'):return
        for record,channel in rows:
            actor=next((key for key,name in actors.ROLES.items() if record.get('speaker')==name),None)
            _observe(data,actor,record['content'],record['id'],record['created_at'],channel,record['role'])
        data['physical_events_initialized']=True
    store.mutate(user,world,replay)


def knowledge(user,world,actor,public=False):
    data=storage.get_store().read(user,world)
    meal=data.get('household_meal',{})
    result={}
    cook_site=_site(data,meal['cook']) if meal.get('cook') in actors.ROLES else None
    witnessed=actor in meal.get('observers',[]) or (meal.get('shareable') and cook_site and cook_site==_site(data,actor))
    if meal and 0<=_clock(data)-meal.get('story_minute',0)<=180 and witnessed and (not public or meal.get('shareable')):
        result['meal']={key:meal[key] for key in ('food','state','cook','basis')}
        result['meal']['cook']=actors.ROLES[meal['cook']]
    user_presence=data.get('physical_presence',{}).get('user',{})
    if not public and actor in user_presence.get('known_to',[]):result['user_location']=user_presence['site']
    if not public:
        site=_site(data,actor)
        if site:result['same_home_roles']=[actors.ROLES[key] for key in actors.ROLES if _site(data,key)==site]
    return result


def delivery(user,world,actor,target,message_id,text,stamp):
    if target!='group':return
    def update(data):
        notices=data.setdefault('group_delivery_receipts',[])
        if not any(n['message_id']==message_id for n in notices):
            notices.append({'actor':actor,'message_id':message_id,'created_at':stamp,
                            'meal_call':bool(re.search(r'吃饭|吃面|面好了|饭好了|来吃|吃粥|开饭',text))})
        data['group_delivery_receipts']=notices[-20:]
        meal=data.get('household_meal',{})
        if meal.get('cook')==actor and meal.get('shareable') and notices[-1]['meal_call']:
            meal['observers']=list(actors.ROLES)
    storage.get_store().mutate(user,world,update)


def receipts(user,world,actor):
    data=storage.get_store().read(user,world)
    return [{**n,'actor':actors.ROLES[n['actor']]} for n in data.get('group_delivery_receipts',[])[-5:]]


def meal_conflict(text,facts):
    meal=facts.get('meal',{})
    return meal.get('state')=='ready' and bool(re.search(r'面还硬|还没熟|没煮好|还没好|水刚开|还在煮|还没做|没做饭',text))


def fake_delivery(text):
    return bool(re.search(r'(?:已经|刚才|刚刚|我)?发了[。！!\s]|(?:已经|我刚).{0,5}发(?:到|在).{0,4}群|发出去[了啦]|发送成功|点(?:了|下)?(?:发送|了发送)|(?:消息|那个圈|发送).{0,12}(?:转圈|在转|灰的|网不好|没信号)',text))


def unsupported_handover(text):
    return bool(re.search(r'说到了|转告到了|当面.{0,6}(?:说过|说了|转告了|告诉了)|(?:已经|刚才|刚刚).{0,6}(?:跟|向).{0,5}(?:小婉|小研|小柳|她).{0,4}说(?:了|过)',text))
