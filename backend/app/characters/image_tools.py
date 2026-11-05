"""Image tool for an already accepted website message; never an intent preflight."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
import json
import re
from backend.app.characters import actors,image_generation
from backend.app.infrastructure import storage
from backend.app.core.context import chat_world

INSTRUCTIONS = """图片请求必须通过 generate_image 实际提供图片，不能仅靠括号动作或台词假装拍照、发送。
根据当前用户话语和最近对话判断：用户让你看看本人、要自拍、嫌上一张只有被子或没拍到人、要求重拍/换角度/补拍时，都属于请求新图片，应调用 generate_image。
例如“让我我看看你呀怎么只有被子”“让我看看你”“那重新拍一张”应生成符合修正要求的新图；同一条用户消息最多一张，新用户消息可以再拍。
用户要看人物本人时，prompt 必须明确本人是主体、脸部或要求的身体部位入镜，不能只拍床、被子或环境。参考之前图片只能依据文字，不要声称已经看懂图片内容。
“你能生图吗”只是能力询问；“不要发图片”是否定；评价照片好看也不要求新图，这些普通回复即可。
绝不能输出 [[action:image]]、[[image:...]] 或在聊天正文附加图片提示词；图片描述只能放进 generate_image 的 prompt 参数。
没有调用工具时，不要写“拍了一张”“重拍”“发过去了”等暗示实际提供新照片的动作或承诺；不要伪造图片链接或 [[image:...]] 标记。
工具在后台拍照，pending/running 不代表照片已完成。"""

TOOL={'type':'function','function':{'name':'generate_image',
    'description':'用户明确要求现在生成、绘制或拍摄图片时调用。结合对话理解“发张当前状态的图片”“看看你现在的样子”。能力询问“你能生图吗”、否定“不要发图片”、引用、讨论已有图片时不要调用。纠正上一张照片（如“怎么只有被子，让我看看你”）、要求本人入镜或重拍，也必须调用工具，不能只在文字里说重来。每条用户消息最多一张，新消息可再次拍照。/image 描述是明确请求。',
    'parameters':{'type':'object','properties':{
        'prompt':{'type':'string','description':'用户要求的完整画面描述，不包含界面或图片标记。'},
        'actor':{'type':'string','enum':['','wan','yan','liu'],'description':'群聊的目标人物；空字符串为纯场景。私聊固定为当前人物。'},
        'size':{'type':'string','enum':list(image_generation.SIZES)}},
        'required':['prompt'],'additionalProperties':False}}}
EXECUTOR=ThreadPoolExecutor(max_workers=2,thread_name_prefix="chat-image")


def collect(calls,deltas):
    for delta in deltas or []:
        index=delta.index
        entry=calls.setdefault(index,{'id':'','name':'','arguments':''})
        entry['id']+=getattr(delta,'id',None) or ''
        function=getattr(delta,'function',None)
        if function:
            entry['name']+=getattr(function,'name',None) or ''
            entry['arguments']+=getattr(function,'arguments',None) or ''
            if len(entry['arguments'])>12000:raise ValueError('图片工具参数过长')


def enqueue(request,calls):
    if len(calls)!=1:raise ValueError('每条消息只能调用一次图片工具')
    call=next(iter(calls.values()))
    if call['name']!='generate_image':raise ValueError('未知聊天工具')
    args=json.loads(call['arguments'])
    if not isinstance(args,dict):raise ValueError('图片工具参数无效')
    prompt=args.get('prompt')
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>4000:raise ValueError('图片描述无效')
    world=chat_world.get();user=storage.scope.get()[0]
    actor=world['channel'] if world['channel']!='group' else args.get('actor','')
    if actor not in ('',*actors.ROLES):raise ValueError('图片人物无效')
    request_id='tool-'+hashlib.sha256(request.encode()).hexdigest()[:48]
    identity=hashlib.sha256(f"{user}:{world['id']}:{world['channel']}:{request_id}".encode()).hexdigest()
    try:return image_generation.get(user,world['id'],world['channel'],identity)
    except PermissionError:pass
    return image_generation.enqueue(user,world['id'],world['channel'],actor,prompt,
        args.get('size','1024x1024'),request_id,record_user=False)


def start(job):
    if job['status']!='pending':return
    # Inbox batches may run in short-lived asyncio loops. A dedicated executor
    # keeps image work independent from their loop shutdown and cancellation.
    EXECUTOR.submit(copy_context().run,image_generation.process,job['id'])


async def reply(cfg,payload,calls,result):
    """Let the same role continue naturally after receiving the real tool result."""
    from backend.app.core import services
    from backend.app.infrastructure import model_usage
    tool_calls=[{'id':call.get('id') or 'image_call_'+str(index),'type':'function',
        'function':{'name':call['name'],'arguments':call['arguments']}} for index,call in calls.items()]
    messages=[*payload,{'role':'assistant','content':None,'tool_calls':tool_calls}]
    messages.extend({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result,ensure_ascii=False)} for call in tool_calls)
    messages.append({'role':'system','content':
        '根据工具真实结果继续回应最新用户，保持本人的性格、说话方式和当前情境。简短自然，可以带动作或情绪。'
        '不要复述提示词或画面描述，不提工具、任务、排队、模型、系统等实现细节，不用“正在生成图片，完成后会发到这里”这类客服播报。'
        'pending/running 只表示图片尚未完成，不要说照片已发送或已到达；done 才是图片已就绪；失败时自然说明这次没能提供照片。'})
    client=services.models().make_client(cfg)
    try:
        response=await model_usage.complete(client,cfg,messages,'character',temperature=cfg.get('temperature',1),max_tokens=400)
        return response.choices[0].message.content or ''
    finally:await client.close()


INTERNAL_MARKER=re.compile(r'\[\[\s*(?:action\s*:\s*image|image\s*:)[^\]\n]*\]\]',re.I)


def visible_text(text):
    # Legacy image-action prose places the internal prompt after the marker.
    match=INTERNAL_MARKER.search(text)
    return text[:match.start()].rstrip() if match else text


async def repair(cfg,payload,raw):
    """One bounded retry through real tools; prose markers never directly spend money."""
    from backend.app.core import services
    from backend.app.infrastructure import model_usage
    messages=[*payload,{'role':'assistant','content':raw},{'role':'system','content':
        '上一条输出包含无效的内部图片标记，未实际创建图片。重新处理最新用户请求：确实要图片才调用 generate_image；'
        '能力询问、否定等仅正常回答，不调用工具。只用真实工具调用，禁止在正文写标记和提示词。'}]
    client=services.models().make_client(cfg)
    try:
        response=await model_usage.complete(client,cfg,messages,'character',tools=[TOOL],tool_choice='auto',max_tokens=1200)
        message=response.choices[0].message
        calls={i:{'id':call.id,'name':call.function.name,'arguments':call.function.arguments}
            for i,call in enumerate(getattr(message,'tool_calls',None) or [])}
        return message.content or '',calls
    finally:await client.close()


def legacy_photo(text):
    """Read older role plans without publishing their internal image description."""
    match=re.search(r'\[\[\s*action\s*:\s*image\s*\]\]',text,re.I)
    if not match:return ''
    prompt=text[match.end():].strip()
    if prompt.startswith(('（','(')) and prompt.endswith(('）',')')):prompt=prompt[1:-1].strip()
    return prompt if 1<=len(prompt)<=4000 else ''
