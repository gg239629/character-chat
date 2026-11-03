"""Deterministic context selection; source files and canonical history stay intact."""
import re
from contextvars import ContextVar

topic=ContextVar('profile_topic',default=None)


def needs_recall(text):
    return bool(re.search(r'记得|以前|之前|上次|昨天|约定|约好|说好|答应|回忆|承诺|群里|群聊|私聊|私信|转告|发个|发消息|收到|她说|他们|她们|大家|你们|为什么|怎么|告诉|小婉|小研|小柳',text)) or len(text)>100


def select_profile(text,query,limit=2000):
    # Preserve the opening identity and select entire short paragraphs. Never rewrite the archive.
    blocks=[b.strip() for b in re.split(r'\n\s*\n',text) if b.strip()]
    terms=set(re.findall(r'[\u4e00-\u9fff]{2}|[A-Za-z]{3,}',query))
    def score(block,index):
        title=block.splitlines()[0]
        return (30 if index<2 else 0)+(12 if re.search(r'定位|性格|身份|关系|住处|称呼|基本|外貌|相处',title) else 0)+sum(3 for term in terms if term in block)
    selected=[];used=0
    for index,block in sorted(enumerate(blocks),key=lambda item:(-score(item[1],item[0]),item[0])):
        if used+len(block)>limit:continue
        selected.append((index,block));used+=len(block)
    return '\n\n'.join(block for _,block in sorted(selected))


def fit(payload,limit=9000):
    """Keep latest input, persona frame and explicit identity facts; remove old/repeated extras first.

    Limit is characters, not a tokenizer-specific token promise. An oversized latest
    input or explicit persona is preserved even when it alone exceeds the budget.
    """
    if not payload:return payload
    latest=next((i for i in range(len(payload)-1,-1,-1) if payload[i]['role']=='user'),None)
    frame=next((i for i in range(len(payload)-1,-1,-1) if payload[i]['role']=='system' and '你是' in payload[i]['content']),len(payload)-1)
    pinned={0,frame}
    pinned.update([i for i,m in enumerate(payload) if m['role']=='system'][-2:])
    if latest is not None:pinned.add(latest)
    for i,m in enumerate(payload):
        if m['content'].startswith(('明确设定与空间依据：','共同世界场景','当前事实与明确设定：',
                                    '真实通信执行结果','已核实的通信事实')):pinned.add(i)
    kept=set(pinned);used=sum(len(payload[i]['content']) for i in kept)
    # Newest raw dialogue has priority over optional recalled memory/evidence.
    order=[i for i in range(len(payload)-1,-1,-1) if payload[i]['role']!='system']
    order += [i for i in range(1,len(payload)) if payload[i]['role']=='system']
    seen={payload[i]['content'] for i in kept}
    for i in order:
        text=payload[i]['content']
        if i in kept or text in seen:continue
        if used+len(text)<=limit:kept.add(i);seen.add(text);used+=len(text)
    return [m for i,m in enumerate(payload) if i in kept]


def dialogue_window(messages):
    """4 rounds normally, up to 8 for references, coordination or unfinished work.

    Whole old rounds are removed first. The current round remains intact, even
    if oversized; the overall payload fitter subsequently prioritizes latest input.
    """
    from backend.app.characters.role_routing import recent_rounds
    latest=next((m.get('content','') for m in reversed(messages) if m.get('role')=='user'),'')
    followup=bool(re.search(r'为什么|怎么|刚才|前面|之前|上次|约定|约好|说好|继续|接着|然后呢|那件事|这件事|那个|这个|她说|他说|群里|群聊|私聊|私信|转告|发消息|收到|你们|大家|各自|分别',latest))
    ongoing=any(re.search(r'等你|等她|还没发|尚未|正在|稍后|一会儿|处理完|轮到',m.get('content','')) for m in messages[-4:])
    expanded=followup or ongoing
    selected=recent_rounds(messages,8 if expanded else 4)
    budget=5500 if expanded else 3500
    while sum(len(m.get('content','')) for m in selected)>budget:
        starts=[i for i,m in enumerate(selected) if m.get('role')=='user']
        if len(starts)<2:break
        selected=selected[starts[1]:]
    return selected
