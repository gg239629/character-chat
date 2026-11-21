"""Deterministic context selection; source files and canonical history stay intact."""
import json
import re


def needs_recall(text):
    return bool(re.search(r'记得|以前|之前|上次|昨天|约定|约好|说好|答应|回忆|承诺|计划|预约编号|改到|取消|群里|群聊|私聊|私信|转告|发个|发消息|收到|她说|他们|她们|大家|你们|为什么|怎么|告诉|小婉|小研|小柳',text)) or len(text)>100


def brief_continuation(text):
    return bool(re.fullmatch(
        r'\s*(早上好|早安|午安|晚上好|晚安|你好|嗨|哈喽|好呀|好的|好|嗯嗯|嗯|行|可以|收到|谢谢|你在干嘛)[\s。！!？?～~]*',
        text,
    ))


def _profile_blocks(text):
    blocks = []
    heading = []
    for block in re.split(r'\n\s*\n', text.strip()):
        block = block.strip()
        if not block:
            continue
        if re.fullmatch(r'#{1,6} [^\n]+', block):
            heading.append(block)
            continue
        blocks.append('\n\n'.join([*heading, block]))
        heading.clear()
    return blocks


def _pick_blocks(blocks, scores, limit):
    selected = []
    used = 0
    for index in sorted(scores, key=lambda index: (-scores[index], index)):
        size = len(blocks[index]) + (2 if selected else 0)
        if used + size <= limit:
            selected.append(index)
            used += size
    return sorted(selected)


def profile_parts(text, query, limit):
    """Select core identity independently of the turn, then matching extra paragraphs."""
    from backend.app.memory.long_memory import terms

    blocks = _profile_blocks(text)
    core_scores = {}
    for index, block in enumerate(blocks):
        heading = block.split('\n\n', 1)[0].splitlines()[0]
        score = (30 if index < 2 else 0)
        if re.search(r'定位|性格|身份|关系|住处|称呼|基本|外貌|相处', heading):
            score += 12
        if score:
            core_scores[index] = score
    core = _pick_blocks(blocks, core_scores, limit * 2 // 3)
    core_text = '\n\n'.join(blocks[index] for index in core)

    query_terms = terms(query)
    matches = {index: len(query_terms & terms(block)) for index, block in enumerate(blocks)
               if index not in core}
    relevant = _pick_blocks(blocks, {i: score for i, score in matches.items()
                                     if score and score >= min(2, len(query_terms))},
                            limit - len(core_text) - (2 if core_text else 0))
    return core_text, '\n\n'.join(blocks[index] for index in relevant)


def select_profile(text, query, limit=2000):
    return '\n\n'.join(part for part in profile_parts(text, query, limit) if part)


def fit(payload,limit=9000):
    """Keep latest input, persona frame and explicit identity facts; remove old/repeated extras first.

    Limit is characters, not a tokenizer-specific token promise. An oversized latest
    input or explicit persona is preserved even when it alone exceeds the budget.
    """
    if not payload:return payload
    required_prefixes = ('你只扮演', '你是', '这是已重置的新故事', '明确设定与空间依据：',
                         '共同世界场景', '当前事实与明确设定：', '真实通信执行结果',
                         '已核实的通信事实', '本轮群聊围绕用户目标')
    kept = {0}
    rounds = []
    for index, message in enumerate(payload):
        if message['role'] == 'system':
            if message['content'].startswith(required_prefixes):
                kept.add(index)
            continue
        if not rounds or message['role'] == 'user':
            rounds.append([])
        rounds[-1].append(index)
    if rounds:
        kept.update((rounds[-1][0], rounds[-1][-1]))
    used = sum(len(payload[index]['content']) for index in kept)

    def add(indexes):
        nonlocal used
        additions = [index for index in indexes if index not in kept]
        size = sum(len(payload[index]['content']) for index in additions)
        if used + size > limit:
            return False
        kept.update(additions)
        used += size
        return True

    for index, message in enumerate(payload):
        if message['role'] == 'system' and message['content'].startswith('本轮关键记忆：'):
            add([index])
    for turn in reversed(rounds):
        if not add(turn):
            break
    for index, message in enumerate(payload):
        if message['role'] == 'system':
            add([index])
    return [message for index, message in enumerate(payload) if index in kept]


def expand_dialogue(messages):
    latest=next((m.get('content','') for m in reversed(messages) if m.get('role')=='user'),'')
    followup=not brief_continuation(latest) and bool(re.search(r'为什么|怎么|刚才|前面|之前|上次|约定|约好|说好|继续|接着|然后呢|那件事|这件事|那个|这个|她说|他说|群里|群聊|私聊|私信|转告|发消息|收到|你们|大家|各自|分别',latest))
    ongoing=any(re.search(r'等你|等她|还没发|尚未|正在|稍后|一会儿|处理完|轮到',m.get('content','')) for m in messages[-4:])
    return followup or ongoing


def dialogue_window(messages):
    """Keep up to four recent rounds, or eight when a reply depends on earlier turns."""
    from backend.app.characters.role_routing import recent_rounds

    expanded = expand_dialogue(messages)
    selected=recent_rounds(messages,8 if expanded else 4)
    budget=5500 if expanded else 3500
    while sum(len(m.get('content','')) for m in selected)>budget:
        starts=[i for i,m in enumerate(selected) if m.get('role')=='user']
        if len(starts)<2:break
        selected=selected[starts[1]:]
    return selected


def message_tokens(message, count):
    extra = {key: value for key, value in message.items() if key not in ('role', 'content')}
    return 4 + count(message['content'] or '') + (
        count(json.dumps(extra, ensure_ascii=False, separators=(',', ':'))) if extra else 0
    )


def rotation_index(history, count, budget):
    """Start a new segment at a user turn after crossing the token warning line."""
    sizes = [message_tokens(message, count) for message in history]
    total = sum(sizes)
    if total <= budget * .90:
        return 0
    for index, message in enumerate(history):
        if index and message['role'] == 'user' and total <= budget * .55:
            return index
        total -= sizes[index]
    # A large recent turn may use most of the available history.
    return next((i for i in range(len(history) - 1, 0, -1) if history[i]['role'] == 'user'), 0)


def retained_history_start(history, count, remaining):
    # This hard-limit fallback also handles a single large historical turn.
    # Cut whole old turns, keeping the same order for every retained message.
    history_size = sum(message_tokens(m, count) for m in history)
    start = 0
    while history_size > remaining and start < len(history):
        end = next((i for i in range(start + 1, len(history)) if history[i]['role'] == 'user'), len(history))
        history_size -= sum(message_tokens(m, count) for m in history[start:end])
        start = end
    return start, history_size


def token_payload(prefix, history, tail, current, count, limit, *, required=()):
    """Preserve an append-only transcript and budget the entire input."""
    if isinstance(current, dict):
        current = [current]
    required = list(required)
    remaining = limit - sum(message_tokens(m, count) for m in [*prefix, *required, *current])
    if remaining < 0:
        raise ValueError('当前消息和必需的上下文超过模型输入预算，请缩短消息或调整上下文设置。')
    start, history_size = retained_history_start(history, count, remaining)
    remaining -= history_size
    kept_tail = []
    for message in tail:
        size = message_tokens(message, count)
        if size <= remaining:
            kept_tail.append(message)
            remaining -= size
    return [*prefix, *history[start:], *required, *kept_tail, *current]
