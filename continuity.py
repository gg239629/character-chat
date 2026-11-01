"""Explicit identity and evidenced positions, independent of dialogue generation."""
import re
import storage
import actors


def is_plan(text):
    return bool(re.search(r'准备|打算|计划|想要|如果|假如|要是|假装|装作|并未|没有|还没|明天|昨天|即将|正要|要去|待会|等会',text))


def capture(user,world,channel,record):
    text=record.get('content','');changes={};position=None
    actor='user' if record.get('role')=='user' else next((r for r,n in actors.ROLES.items() if n==record.get('speaker')),None)
    if not actor:return
    if actor=='user' and not re.search(r'[“”「」"?？]|如果|假如|扮演|假装|我说过|他说|她说',text):
        gender=re.search(r'(?:^|[，,。\s])我(?:其实|本来)?是(?:个|一名|一个)?(男的|女的|男性|女性|男人|女人|男生|女生)[呀啊呢]?(?:[，,。！!\s]|$)',text)
        if gender:changes['gender']='男' if gender[1].startswith('男') else '女'
        name=re.fullmatch(r'(?:我叫|我的名字是)([\w\u4e00-\u9fff]{1,16})[。！! ]*',text.strip())
        if name:changes['name']=name[1]
    narratives=re.findall(r'[（(]([^（）()]*)[）)]',text)
    for action in narratives:
        if is_plan(action):continue
        relative=re.search(r'(?:走到|站到|坐到|挪到)(小婉|小研|小柳|用户|你)(?:的)?(左侧|右侧|旁边|身旁|对面|身后|前面)',action)
        absolute=re.search(r'(?:走到|来到|站到|坐到|走进|回到|进入)(客厅|厨房|卧室|书房|阳台|玄关|门口|窗边|餐桌旁|沙发旁|车内)',action)
        if absolute:position={'place':absolute[1],'relation':None,'relative_to':None}
        if relative:
            target=next((r for r,n in actors.ROLES.items() if n==relative[1]),'user')
            if target!=actor:position={'place':absolute[1] if absolute else None,'relative_to':target,'relation':relative[2]}
    if not changes and not position:return
    def update(data):
        if changes:
            profile=data.setdefault('user_profile',{})
            for key,value in changes.items():profile[key]={'value':value,'source_id':record['id'],'updated_at':record['created_at']}
        if position:
            positions=data.setdefault('role_positions',{})
            if record['created_at']<=positions.get(actor,{}).get('updated_at',0):return
            known=list(actors.ROLES) if channel=='group' else [channel] if channel in actors.ROLES else [actor]
            positions[actor]={**position,'source_id':record['id'],'updated_at':record['created_at'],'basis':'actual_action','known_to':known}
    storage.get_store().mutate(user,world,update)


def context(user,world,actor):
    data=storage.get_store().read(user,world)
    positions=data.get('role_positions',{})
    own=positions.get(actor)
    cutoff=max(data.get('role_states',{}).get(actor,{}).get('evidence_at',0),data.get('world_clock_revision',0))
    if own and own.get('updated_at',0)<cutoff:own=None
    if own and own.get('relative_to') and positions.get(own['relative_to'],{}).get('updated_at',0)>own['updated_at']:
        own={**own,'relative_to':None,'relation':None}
        if not own.get('place'):own=None
    user_position=positions.get('user')
    if user_position and (actor not in user_position.get('known_to',[]) or user_position.get('updated_at',0)<data.get('world_clock_revision',0)):user_position=None
    # Private actions of other roles never become omniscient position hints.
    return {'user_profile':{k:v['value'] for k,v in data.get('user_profile',{}).items()},
        'own_position':own,'user_position':user_position,'rule':'用户未明确的设定保持未知。只控制本人动作；相对位置不反向推断他人位置。计划不算完成，不代替别人移动。'}
