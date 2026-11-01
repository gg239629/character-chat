"""Public-only group coordinator: nominates an order, never writes dialogue."""
import asyncio
import json
import re
from contextvars import ContextVar
from pydantic import BaseModel,Field
import actors
import role_actions
import storage
import model_usage

discussion_state=ContextVar('discussion_state',default=None)
# Bound one HTTP generation so an accidental model loop cannot run indefinitely.
MAX_MESSAGES=8
MAX_ATTEMPTS=12


async def speakers(messages,configs,required=()):
    """Reconsider after every accepted message; silence only declines this context."""
    pending=[];declined=set();last=None;emitted=0;required=list(dict.fromkeys(required))
    for _ in range(MAX_ATTEMPTS):
        if emitted>=MAX_MESSAGES:return
        if required:
            role=required.pop(0)
        else:
            if not pending:
                state={'last_speaker':last,'declined_current_context':list(declined),
                       'messages_this_exchange':emitted,'remaining_message_budget':MAX_MESSAGES-emitted}
                token=discussion_state.set(state)
                try:pending=list(await plan(messages,configs))
                finally:discussion_state.reset(token)
            pending=[role for role in pending if role in actors.ROLES and role not in declined and role!=last]
            if not pending:return
            role=pending.pop(0)
        before=len(messages)
        yield role
        if len(messages)>before:
            emitted+=1;last=role;declined.clear();pending=[]
        else:
            declined.add(role)


class Plan(BaseModel):
    order:list[str]=Field(max_length=3)


def public_cards(user,world):
    data=storage.get_store().read(user,world)
    # Public traits are deliberately separate from private persona/relationship fields.
    cards={}
    for role in actors.ROLES:
        with actors.role_scope(user,world,role):
            cards[role]=storage.read_document('public_persona',{'wan':'亲近直接，愿意表达自己的看法','yan':'细致敏感，熟悉的话题会认真回应','liu':'独立自然，有想法时接话'}[role])
    return data,cards


async def plan(messages,configs):
    import server
    state=discussion_state.get()
    if state and messages:
        latest=messages[-1]
        body=re.sub(r'^\s*小[婉研柳][：:]','',latest.get('content','')).strip()
        # Only explicit direct questions qualify; indirect mentions still use the coordinator.
        addressed=next((role for role,name in actors.ROLES.items() if re.match(r'^'+name+r'[，,:：\s]',body)),None)
        if addressed and re.search(r'[?？]|你呢|你觉得|你看|几点|怎么|为什么|好吗|行吗|吗[。！!]?$',body):
            if addressed!=state.get('last_speaker') and addressed not in state.get('declined_current_context',[]):return [addressed]
        if latest.get('role')=='assistant' and state.get('messages_this_exchange',0)>=2 and re.fullmatch(r'(?:好的?|嗯嗯?|行|好呀|那就这样|就这么定了|到时见|晚安)[，。！!\s～~]*',body):return []
    world=server.chat_world.get()
    user=storage.scope.get()[0]
    data,cards=await asyncio.to_thread(public_cards,user,world['id'])
    cfg=model_usage.group_config()
    recent=[];remaining=4200
    for message in reversed(messages[-6:]):
        text=message.get('content','')
        if not text:continue
        if len(text)>remaining:text=text[-remaining:]
        recent.append({'role':message['role'],'content':text});remaining-=len(text)
        if remaining<=0:break
    topic=next((m.get('content','')[-600:] for m in reversed(messages) if m.get('role')=='user'),'')
    context={'roles':actors.ROLES,'public_traits':cards,
             'public_scene':{key:value for key,value in data.get('scene_state',{}).items() if key in ('时间','地点','在场人物')},
             'topic':topic,'group_messages':list(reversed(recent))}
    said={role:[] for role in actors.ROLES}
    for message in messages[-20:]:
        if message.get('role')!='assistant':continue
        match=re.match(r'^\s*(小婉|小研|小柳)[：:]',message.get('content',''))
        if not match:continue
        role=next(r for r,n in actors.ROLES.items() if n==match[1])
        spoken=re.sub(r'[（(][^（）()]*[）)]','',message['content'][match.end():]).strip()
        if spoken:said[role].append(spoken[:100])
    context['already_said']={r:items[-2:] for r,items in said.items() if items}
    if discussion_state.get() is not None:context['discussion']=discussion_state.get()
    try:
        with model_usage.scope('coordinator'):
            value=await asyncio.wait_for(role_actions.ask_json(cfg,
            '你是微信群聊公开调度者，只安排候选角色和顺序，不写台词、不猜私聊秘密。'
            '依公开话题、已发言内容、公开性格及自然接话需要选择0至3个不同角色。'
            '点名不是强制回答，群体提问也不代表必须三人发言。没有合适回应可以空数组。'
            '不能按固定名单轮流，优先与当前话题和前面发言相关的人。'
            '这是动态接话，每条实际发言后都会重新判断；order只表示此刻谁最想接下一句，不是本轮固定名单。'
            '重点看最后一条消息说给谁、是否提问、邀请、反驳或需要回应。角色可以回应另一角色，不必每句都答用户。'
            'topic只是开场背景，already_said是已说内容；不能反复提名角色重答开场问题或换词重报状态。'
            '确认、提醒、道谢没有产生新问题就结束，点名致谢不构成必须接话的理由；不串成逐人确认链。'
            '同一角色先前说过话也可再次被提名，尤其被其他人追问、点名或有新观点时；不要连续提名最后发言的人。'
            'declined_current_context中的角色在没有新消息前已经选择沉默，不重复提名。'
            '没有待回应的问题、新观点或自然接话缘由时输出空数组，简短确认或话题收尾后不要强行延长，更不能为了三人齐全而凑发言。'
            '你只是提名，角色本人仍可根据其经历沉默。只输出JSON {"order":["wan","yan","liu"]}，'
            '数组元素必须来自wan/yan/liu，可不同顺序或更少，不输出对白或隐私推理。',context),timeout=25)
        parsed=Plan.model_validate(value)
        if len(set(parsed.order))!=len(parsed.order) or any(role not in actors.ROLES for role in parsed.order):
            raise ValueError('Invalid participants')
        return parsed.order
    except Exception:
        # Provider failures degrade to each role deciding, never to forced dialogue.
        return actors.candidates(messages)
