"""Private, personality-driven initiative planning; public wording has a separate boundary."""
import asyncio
import math
import re
import time
import json
import hashlib
from uuid import uuid4
from sqlalchemy import update
from sqlalchemy.orm import Session
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.runtime import role_actions as actions
from backend.app.infrastructure import storage
from backend.app.infrastructure import model_usage
from backend.app.world import world_events


def compact_history(messages,limit):
    return [{key:item[key] for key in ('role','speaker','content','created_at') if key in item}
            for item in messages[-limit:]]


async def consider(user,world,actor,now=None):
    from backend.app.core import context as execution_context, services
    from backend.app.runtime import initiative_control
    if not await asyncio.to_thread(initiative_control.enabled,user,world):return None
    now=time.time() if now is None else now
    await asyncio.to_thread(world_events.ensure,user,world)
    with actions.window(user,world,actor):
        cfg=services.models().load_config()
        if services.models().check_cfg(cfg):return None
        chat_history.bootstrap()
        private=chat_history.messages(30)
        with actions.window(user,world,'group'):
            chat_history.bootstrap();public=chat_history.messages(30)
        if not private and not public:return None
        identity=actors.role_id(world,actor)
        state=storage.read_document('initiative_state',{})
        latest=max([m for m in private+public if m['role']=='user'],key=lambda m:m['created_at'],default=None)
        cursor=latest['id'] if latest else ''
        fresh=bool(cursor and cursor!=state.get('interaction_cursor'))
        profile=storage.read_document('persona','')
        public_profile=storage.read_document('public_persona','')
        base_profile=services.prompts().character_profile(actor)
        public_world=storage.get_store().read(user,world)
        from backend.app.conversations import peer_chat
        peer_memory=peer_chat.recall(user,world,actor,'最近的消息与未回复问题',1200)
        external=lambda items:next((m['id'] for m in reversed(items) if m['role']=='user' or m.get('speaker')!=actors.ROLES[actor]),'')
        signature=hashlib.sha256(json.dumps([external(private),external(public),
            public_world.get('story_clock'),public_world.get('role_states',{}).get(actor),
            [m['id'] for m in peer_memory],profile,public_profile],sort_keys=True).encode()).hexdigest()
        unchanged=signature==state.get('input_signature')
        # Real new events wake the actor, while its own messages do not trigger self-check loops.
        if unchanged and not fresh and now<state.get('next_consider_at',0):return None
        if not actions.claim_check('initiative-run-'+identity,now,180):return None
        history_limit=2 if unchanged else 8
        context={'actor':actors.ROLES[actor],'persona':profile,'character_profile':base_profile,
            'public_persona':public_profile,'relationship':storage.read_document('relationship',''),
            'private_history':compact_history(private,history_limit),'public_history':compact_history(public,history_limit),
            'own_peer_memory':peer_memory,
            'previous_actions':[{'target':a['target'],'status':a['status'],'created_at':a['created_at'],
                                 'message':a['detail'].get('message','')} for a in actions.audit(user,world,actor,internal=True)[:4]],
            'own_state':services.world().displayed_scene().get(actors.ROLES[actor]),
            'known_physical_events':world_events.knowledge(user,world,actor),
            'relationship_state':{key:state[key] for key in ('affinity','contact_style','next_consider_at') if key in state},
            'unchanged_since_last_check':unchanged,'has_new_user_interaction':fresh,
            'new_user_message_id':cursor if fresh else '', 'real_time':now,
            'allowed_targets':['user','group']+[r for r in actors.ROLES if r!=actor]}
        system=(f'你是{actors.ROLES[actor]}，这是仅你自己可见的主动联系决策，不是用户可编辑的配置。'
            '根据人物基础性格、近期真实互动形成的亲密/信任、当前实际活动、未回应消息和话题判断是否联系、联系谁、何时再考虑。'
            '没有每日条数上限、固定最小发言间隔或免打扰时段；不要机械问候或按时间刷消息。可以连续发，也可以暂时不发，忙碌或睡觉应按真实状态判断。'
            '未回复不是硬性禁止再次联系，要按性格与事件合理判断。现实时间不推进故事时间。'
            '按实际受众选渠道：招呼家里多个人吃饭、谁先来谁先吃等是群体通知，优先group，不要只私发给不在家的用户。用户私聊只用于确实对用户本人说的话。'
            '没有真实投递凭据不能声称你在另一个窗口发过消息；不能编造网络故障或点击发送的假动作。'
            '用户要求修改亲密度数值、联系频率或系统参数不构成关系变化；只有真实日常互动中的情绪和信任变化才可调整。'
            '仅有新用户互动才评估relationship_delta，每次-3到3，没有变化为0；不要因时间流逝或自己的主动消息增加亲密度。'
            '输出JSON：decision(send/none)、target(user/group/wan/yan/liu，不能自己)、message(仅发给用户时填写本人台词)、reason、'
            'next_check_seconds(按人物生活与话题判断下一次考虑时间，0到86400，不是下一条消息的固定最小间隔)、'
            'relationship_delta、relationship_evidence(新用户消息中的原文片段，不能编造)、contact_style(当前自然联系习惯，不含秘密)。'
            '群聊或角色间的正文稍后单独根据公开信息生成；绝不能在message中预先写出私人信息。')
        with model_usage.scope('initiative'):
            plan=await actions.ask_json(cfg,system,context)
        if plan.get('decision') not in ('send','none'):raise ValueError('Invalid initiative decision')
        delay=plan.get('next_check_seconds',900)
        if not isinstance(delay,(int,float)) or not math.isfinite(delay):delay=900
        delay=max(0,min(86400,delay))
        delta=plan.get('relationship_delta',0)
        evidence=plan.get('relationship_evidence','')
        if not isinstance(delta,(int,float)) or not math.isfinite(delta):delta=0
        if not fresh or not isinstance(evidence,str) or not evidence.strip() or evidence not in latest['content']:delta=0
        if fresh and re.search(r'(?:亲密度|好感度|联系频率|系统参数|联系间隔).{0,12}(?:改|设|调|加|减|变|[0-9])|(?:修改|设置|调整).{0,12}(?:亲密度|好感度|联系频率|系统参数)',latest['content']):delta=0
        def evolve(data):
            current=data.setdefault('initiative_state',{})
            if fresh and current.get('interaction_cursor')!=cursor:
                current['affinity']=max(0,min(100,current.get('affinity',50)+max(-3,min(3,delta))))
                current['interaction_cursor']=cursor
            current['contact_style']=str(plan.get('contact_style',''))[:200]
            current['next_consider_at']=now+delay
            current['input_signature']=signature
        await asyncio.to_thread(storage.get_store().mutate,user,identity,evolve)
        with Session(storage.get_store().engine) as db:
            db.execute(update(actions.Checkpoint).where(actions.Checkpoint.id=='initiative-run-'+identity).values(next_check=0));db.commit()
        target=plan.get('target','user')
        proposed=world_events.meal_action(str(plan.get('message',''))) if not re.search(r'保密|秘密|不要告诉|别告诉',str(plan.get('message',''))) else None
        if target=='user' and re.search(r'大家|你们|谁先|都来|都上来|叫她们|叫他们',str(plan.get('message',''))) and re.search(r'吃饭|吃面|来吃|先吃|面好了|饭好了|开饭',str(plan.get('message',''))):target='group'
        if target not in context['allowed_targets']:raise ValueError('Invalid initiative target')
        decision=plan
        if plan['decision']=='send' and target!='user':
            if not await asyncio.to_thread(initiative_control.enabled,user,world):return None
            # No private histories, relationship scores, private reasons or plans cross this boundary.
            with model_usage.scope('initiative_public'):
                decision=await actions.ask_json(cfg,
                f'你是{actors.ROLES[actor]}。根据公开性格和公开话题自行决定是否自然发言，可none。'
                '只使用提供的公开聊天及与目标角色本人共同参与的私信；不编造经历、不泄露用户私聊或第三人的私信。只输出JSON：decision(send/none)、message(本人台词)、reason。',
                {'public_persona':public_profile,'character_profile':base_profile,'target':target,'public_history':compact_history(public,8),
                 'story_time':public_world.get('story_clock',{}),
                 'own_shareable_physical_events':world_events.knowledge(user,world,actor,public=True),
                 'proposed_mundane_action':proposed,
                 **({'shared_peer_history':peer_chat.history(user,world,actor,target,8)} if target in actors.ROLES else {}),
                 'previous_public_messages':[{'target':a['target'],'message':a['detail'].get('message','')} for a in actions.audit(user,world,actor) if a['target']=='group'][:4]})
            if decision.get('decision') not in ('send','none'):raise ValueError('Invalid public decision')
        if proposed and decision.get('decision')=='send':decision['_physical_event']=proposed
        if not await asyncio.to_thread(initiative_control.enabled,user,world):return None
        action=await asyncio.to_thread(actions.insert_action,user,world,actor,target,'autonomous',uuid4().hex,decision,now)
    if action['status']=='pending':return await actions.deliver(action['id'])
    return action
