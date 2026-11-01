"""Short physical state labels; private updates cannot edit somebody else's state."""
import re


def has_action_evidence(messages, name, implicit=False):
    from continuity import is_plan
    """Conservative gate: dialogue alone cannot move a person's physical state."""
    uncertain=r'撒谎|谎称|骗|假装|装作|借口|开玩笑|没有|并未|没动|不想|打算|准备|计划|如果|假如|要是|明天|昨天|刚才说|声称|说自己'
    action=r'走|回到|来到|到家|进屋|进入|进门|离开|坐|站|躺|起床|跑|煮|做饭|吃|喝|读|看书|浇|洗|睡|在|停稳|停车|熄火|熄了火|拉手刹|换鞋|换了鞋|解开安全带|下车|倒车'
    for message in messages:
        text=message.get('content','')
        if message.get('role')=='user':
            for clause in re.split(r'[。！？\n]',text):
                if re.search(r'我(?:已经|现在|刚刚|刚)?(?:到家|回到家)',clause) and not re.search(r'我们|咱俩|咱们|'+name,clause):continue
                if re.search(r'我(?:已经|现在|刚刚|刚)?(?:到家|回到家)',clause) and not re.search(r'我们|咱俩|咱们|'+name,clause):continue
                if (name in clause or (implicit and re.search(r'我们|咱俩|咱们|到家[了啦]|到家了进屋',clause))) and not re.search(uncertain+r'|[?？“”「」]|说|告诉|让|请|吧|吗',clause) and re.search(action,clause):return True
        elif message.get('role')=='assistant':
            # Only this speaker's explicit action narration; quoted phone speech is excluded.
            chunks=re.split(r'(?=小[婉研柳][：:])',text)
            for chunk in chunks:
                if not chunk.startswith((name+'：',name+':')):continue
                for narrative in re.findall(r'[（(]([^（）()]*)[）)]',chunk):
                    if is_plan(narrative):continue
                    if not re.search(uncertain,narrative) and re.search(action,narrative):return True
    return False


def physical_transition(messages,name,implicit=False):
    from continuity import is_plan
    """Only explicit completed transitions; no plans, speech claims or guesses."""
    result=None
    for message in messages:
        text=message.get('content','')
        if message.get('role')=='user':
            if re.search(r'我(?:已经|现在|刚刚|刚)?(?:到家|回到家)',text) and not re.search(r'我们|咱俩|咱们|'+name,text):continue
            if not (name in text or (implicit and re.search(r'我们|咱俩|咱们|到家[了啦]',text))):continue
            chunks=[text]
        else:
            if not text.startswith((name+'：',name+':')):continue
            chunks=re.findall(r'[（(]([^（）()]*)[）)]',text)
        for chunk in chunks:
            if is_plan(chunk):continue
            if re.search(r'如果|假如|要是|准备|打算|计划|明天|昨天|假装|装作|骗|谎|还没|没到|没停|[?？“”「」]|你说|她说',chunk):continue
            if re.search(r'到家[了啦]|回到家|我们现在到家|咱们到家',chunk):result='家门口·刚到家'
            if re.search(r'停稳|拉手刹|熄了?火|解开安全带',chunk):result='车内·已停车'
            if re.search(r'换了?鞋',chunk):result='玄关·换鞋'
            elif re.search(r'进屋|走进家|进了家门|提进来',chunk):result='家里·刚进门'
    return result


def apply_transition(user,world,channel,record):
    import storage
    import actors
    roles=[channel] if channel in actors.ROLES else list(actors.ROLES)
    changes={role:physical_transition([record],actors.ROLES[role],channel==role) for role in roles}
    changes={r:s for r,s in changes.items() if s}
    if not changes:return
    def apply(data):
        states=data.setdefault('role_states',{})
        for role,status in changes.items():
            old=states.get(role,{})
            if old.get('evidence_at',0)>=record['created_at']:continue
            states[role]={'status':status,'source':'explicit_action','evidence_at':record['created_at'],
                'source_id':record['id'],'updated_at':record['created_at']}
            data.setdefault('scene_state',{})[actors.ROLES[role]]=status
    storage.get_store().mutate(user,world,apply)


def label(value, fallback='位置未知·待确认'):
    if isinstance(value,dict):
        place=value.get('地点',value.get('location',''))
        action=value.get('动作',value.get('activity',''))
        if isinstance(place,str) and isinstance(action,str) and place.strip() and action.strip():
            text=place.strip()+'·'+action.strip()
        else:return label(fallback)
    elif isinstance(value,str):text=re.sub(r'\s+','',value).strip('，,。;；')
    else:return label(fallback)
    if text and len(text)<=10:return text
    if fallback!=value:return label(fallback)
    return '位置未知·待确认'


def ground_location(messages,name,proposed,old):
    proposed=label(proposed,old)
    old_place=old.split('·')[0] if '·' in old else ''
    new_place=proposed.split('·')[0] if '·' in proposed else ''
    if not old_place or '未知' in old_place or old_place==new_place:return proposed
    from continuity import is_plan
    for message in messages:
        text=message.get('content','')
        chunks=re.findall(r'[（(]([^（）()]*)[）)]',text) if text.startswith((name+'：',name+':')) else [text] if message.get('role')=='user' and name in text else []
        if any(not is_plan(c) and new_place in c and (re.search(r'走进|来到|回到|到了|走到|进入|离开|移到|跑到',c)
            or (message.get('role')=='user' and re.search(name+r'(?:已经|现在)?在'+re.escape(new_place),c))) for c in chunks):return proposed
    action=proposed.split('·',1)[-1]
    return label(old_place+'·'+action,old)


# 共用规则（1.0–1.2 行为与格式、1.4 身高住处、1.6 三人规约）与三人档案各 12 节：
# 定位、外貌、性格、说话、性欲体质、敏感点、发情阶梯、床上偏好、色气规则、台词库、禁写、事后。
TRAIT_SECTIONS={'1.0','1.1','1.2','1.4','1.6','1.7'}|{f'{role}.{part}' for role in (2,3,4) for part in range(1,13)}


def fresh_profile(text):
    """Trim an archive down to character traits, dropping story history.

    Prompt assembly no longer calls this: a reset keeps every archive section intact and only
    clears database chat rows. Kept for explicit trimming and for the unit test.
    """
    allowed=TRAIT_SECTIONS
    lines=[];include=False
    for line in text.splitlines():
        if re.match(r'^#{1,6}\s',line):
            match=re.match(r'^#{1,6}\s+(\d+\.\d+)\b',line)
            include=bool(match and match[1] in allowed)
        if include:lines.append(line)
    return '\n'.join(lines).strip()
