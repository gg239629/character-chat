"""One monotonic story clock; private conversation content never enters offscreen routines."""
import re
from backend.app.characters import actors
from backend.app.world import scene_labels

PERIODS={'凌晨':0,'清晨':360,'早晨':480,'早上':480,'上午':600,'中午':720,'下午':900,'傍晚':1080,'晚上':1200,'深夜':1380,'夜里':1320}
ROUTINES={
 'wan':[(0,'卧室·睡觉'),(420,'卧室·起床'),(540,'厨房·收拾'),(720,'餐桌·吃午饭'),(840,'客厅·整理'),(1080,'厨房·做晚饭'),(1200,'客厅·休息'),(1380,'卧室·睡觉')],
 'yan':[(0,'卧室·睡觉'),(420,'卧室·起床'),(540,'书房·看书'),(720,'餐桌·吃午饭'),(840,'书房·学习'),(1080,'餐桌·吃晚饭'),(1200,'书房·阅读'),(1380,'卧室·睡觉')],
 'liu':[(0,'卧室·睡觉'),(420,'卧室·起床'),(540,'阳台·照料花草'),(720,'餐桌·吃午饭'),(840,'客厅·忙自己的'),(1080,'厨房·准备晚饭'),(1200,'客厅·休息'),(1380,'卧室·睡觉')]}


def clock(data):
    if data.get('realtime_clock'):
        value=data['story_clock']
        return {**value,'wall_time':data['realtime_clock']['minute']}
    value=data.get('story_clock')
    if isinstance(value,dict) and isinstance(value.get('day'),int) and isinstance(value.get('minute'),int):
        return {'day':max(1,value['day']),'minute':max(0,min(1439,value['minute']))}
    text=str(data.get('scene_state',{}).get('时间','第一天早晨08:00'))
    return parse(text,{'day':1,'minute':480})


def parse(text,old):
    day=old['day'];minute=old['minute']
    match=re.search(r'第\s*(\d+|[一二三四五六七八九十]+)\s*天',text)
    if match:
        value=match[1]
        if value.isdigit():day=int(value)
        else:
            numbers={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
            if value in numbers:day=numbers[value]
            elif len(value)==2 and value[0]=='十':day=10+numbers.get(value[1],0)
    elif re.search(r'后天',text):day+=2
    elif re.search(r'第二天|次日|翌日|明天',text):day+=1
    digits={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    def chinese_number(match):
        word=match[1]
        if '十' in word:
            left,right=word.split('十',1)
            value=(digits.get(left,1) if left else 1)*10+digits.get(right,0)
        else:value=digits.get(word,0)
        return str(value)+'点'
    text=re.sub(r'([零一二两三四五六七八九十]{1,3})点',chinese_number,text)
    explicit=re.search(r'(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)',text)
    hour=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*点(?:\s*([0-5]?\d)分?)?',text)
    if explicit:
        h=int(explicit[1]);m=int(explicit[2])
        if any(period in text for period in ['下午','傍晚','晚上','夜里']) and h<12:h+=12
        minute=h*60+m
    elif hour:
        h=int(hour[1]);m=30 if text[hour.end():].startswith('半') else int(hour[2] or 0)
        if any(period in text for period in ['下午','傍晚','晚上','夜里']) and h<12:h+=12
        minute=h*60+m
    else:
        for period,value in PERIODS.items():
            if period in text:minute=value;break
    return {'day':max(1,day),'minute':minute}


def phase(value):
    minute=value['minute']
    return (value['day'],0 if minute<360 else 1 if minute<720 else 2 if minute<840 else 3 if minute<1080 else 4 if minute<1200 else 5 if minute<1380 else 6)


def display(value):return value.get('wall_time') or f"第{value['day']}天 {value['minute']//60:02d}:{value['minute']%60:02d}"


def advance_candidate(messages,old,proposed=None):
    if old.get('wall_time'):return old
    # Only user narration about NOW moves the shared clock. Plans/questions/quotes do not.
    users=[m.get('content','') for m in messages if m.get('role')=='user']
    text=users[-1] if users else ''
    if not text:return old
    matches=list(re.finditer(r'快进|聊到|天黑了|天亮了|转眼|时间来到|时间到了|时间已经|现在(?:是|已经|都)?|已经(?:是|到)|到了|一晃|转到|跳到|如今|过了[一二三四五六七八九十\d]+(?:个)?小时',text))
    if not matches:return old
    # Do not interpret "明天晚上去吗" or a remembered/quoted time as a current scene.
    if re.search(r'如果|假如|要是|假设|回忆|想起|昨天|等到|等到了|到时候|你说过|你说|他说|她说|她问|“|「|"',text):return old
    evidence=text[matches[-1].start():]
    if re.search(r'计划|约好|打算|准备|到时候|会不会|是不是|吗|？|\?',evidence):return old
    if re.search(r'明天|后天',evidence) and not re.search(r'快进|跳到|转到|时间来到|时间到了|到了',evidence):return old
    delta=re.search(r'过了([一二三四五六七八九十\d]+)(?:个)?小时',evidence)
    if delta:
        numbers={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
        amount=int(delta[1]) if delta[1].isdigit() else numbers.get(delta[1],0)
        total=old['minute']+amount*60
        candidate={'day':old['day']+total//1440,'minute':total%1440}
    elif '天黑了' in evidence:candidate={'day':old['day'],'minute':1200}
    elif '天亮了' in evidence:candidate={'day':old['day']+(1 if old['minute']>=1200 else 0),'minute':420}
    else:candidate=parse(evidence,old)
    return candidate if candidate['day']*1440+candidate['minute']>old['day']*1440+old['minute'] else old


def routine(role,value):
    entries=ROUTINES[role]
    return next((status for start,status in reversed(entries) if start<=value['minute']),entries[0][1])


def apply(data,candidate,active_roles,revision=None):
    if data.get('realtime_clock'):return False
    old=clock(data)
    if revision is not None and revision<data.get('world_clock_revision',0):return False
    if candidate['day']*1440+candidate['minute']<=old['day']*1440+old['minute']:return False
    data['story_clock']=candidate
    data['world_clock_revision']=revision or data.get('world_clock_revision',0)
    data.setdefault('scene_state',{})['时间']=display(candidate)
    # A scene snapshot from before the advance cannot turn the clock back on commit.
    for role in actors.ROLES:
        if phase(old)!=phase(candidate):
            previous=data.get('role_states',{}).get(role,{})
            known=previous.get('status') or data.get('scene_state',{}).get(actors.ROLES[role],'')
            anchored=candidate['day']==old['day'] and known and '未知' not in known and previous.get('source') not in ('offscreen_routine','reset')
            status=scene_labels.label(known if anchored else routine(role,candidate))
            data.setdefault('role_states',{})[role]={**previous,'status':status,'source':previous.get('source','observed_scene') if anchored else 'offscreen_routine','story_time':display(candidate),'updated_at':display(candidate)}
            event={'story_time':display(candidate),'from_time':display(old),'day':candidate['day'],'minute':candidate['minute'],
                   'status':status,'source':'offscreen_routine','visibility':'actor_private','actor_id':role,
                   'text':f"{display(candidate)}，{status}（按日常节奏推进，没有与其他人的互动或私聊信息）"}
            events=data.setdefault('personal_progress',{}).setdefault(role,[])
            events.append(event)
            data['personal_progress'][role]=events[-30:]
    return True
