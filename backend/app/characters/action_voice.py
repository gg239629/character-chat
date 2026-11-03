"""Let the character phrase verified outcomes; never expose internal receipt prose."""
import re
from backend.app.characters import actors


def natural(text):
    return bool(text and not re.search(r'请求已|私信送达|实际发送到|由她自[己行]|转达不代表|投递|回执|已发送到.*聊天',text))


async def phrase(actor,cfg,fact,history=(),group=False,followup=False):
    from backend.app.infrastructure import storage
    from backend.app.core import context as execution_context, services
    from backend.app.runtime import role_actions
    from backend.app.infrastructure import model_usage
    persona=storage.read_document('public_persona' if group else 'persona','')
    with model_usage.scope('action_voice'):
        decision=await role_actions.ask_json(cfg,
            f'你只扮演{actors.ROLES[actor]}，用本人性格和日常聊天口吻表达系统已经核实的结果。'
            '简短自然，可带情绪、调侃或省略，不用客服、工具或工作汇报的口气。'
            '不说“请求已送达”“由她自行决定”“实际发送”“转达不代表完成”，不附括号系统状态。'
            '仅表达verified_fact，不增加动作、理由或其他角色态度；转达成功不等于收件人已执行。'
            '未确认发送不能说发了；没有拒绝记录不能说对方不愿意。'
            '输出JSON：decision(send/none)、message。'+('没有必要再补一句时可none，不反复播报进展。' if followup else '直接简短回应。'),
            {'persona':persona,'character_profile':services.prompts().character_profile(actor),
             'verified_fact':fact,'recent':[{'role':m['role'],'content':m['content']} for m in list(history)[-4:]]})
    if followup and decision.get('decision')=='none':return None
    text=str(decision.get('message','')).strip()
    if not natural(text):raise ValueError('Unnatural receipt wording')
    return actors.normalize_reply(text,actors.ROLES[actor])
