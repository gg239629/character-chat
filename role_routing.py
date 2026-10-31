"""Choose the primary speaker from conversation context."""
import re
NAMES = ("小婉", "小研", "小柳")
LABEL = re.compile(r"^\s*(小婉|小研|小柳)\s*[：:]", re.MULTILINE)

def last_speaker(messages):
    for message in reversed(messages):
        if message.get("role") == "assistant":
            matches = LABEL.findall(message.get("content") or "")
            if matches:
                return matches[0]
    return None

def reply_targets(messages):
    content = next((m.get("content", "") for m in reversed(messages)
                    if m.get("role") == "user"), "")
    named = [name for name in NAMES if name in content]
    group = any(word in content for word in ("你们", "大家", "各自", "每个人", "所有人", "都说说"))
    return named, group


def select_speaker(messages, memory_messages):
    named, group = reply_targets(messages)
    if len(named) == 1 and not group:
        return named[0]
    return last_speaker(messages) or last_speaker(memory_messages) or (named[0] if named else None)


def reply_instruction(messages, memory_messages):
    named, group = reply_targets(messages)
    speaker = select_speaker(messages, memory_messages)
    if group or len(named) > 1:
        scope = "、".join(named) if named else "当前场景中被问到的在场角色"
        return (f"本轮是多人提问，回复范围：{scope}。结合scene_state判断在场情况，"
                "让被问到且在场的角色分别回应，不用一个人代表大家。允许三个人开口；"
                "各自只说与自己有关的内容，不编造未在场角色的回答。每个角色标注姓名。")
    if speaker:
        return (f"本轮主要回复角色：{speaker}。第一条台词以「{speaker}：」开头。"
                "明确点名单人时由被问的人回答；普通续聊沿用最近主要角色，其他在场角色可自然补充。"
                "不强行凑人数，每条台词标注角色姓名。")
    return "按当前提问对象和在场情况自然回应，每条台词标注角色姓名。"


def recent_rounds(messages, limit):
    """A round begins with a user message and includes following replies.

    An unanswered current user message counts as the newest round.
    """
    if limit <= 0:
        return []
    starts = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    if len(starts) > limit:
        return messages[starts[-limit]:]
    return list(messages)


def merge_recent_history(database, browser, limit=10):
    """Merge an ordered saved prefix with live browser history, then cap rounds.

    Match contiguous messages, not globally unique texts: repeated greetings in
    different rounds must remain. Speaker prefixes are optional in saved text.
    """
    def clean(messages):
        return [{"role": m["role"], "content": m["content"]} for m in messages
                if m.get("role") in ("user", "assistant") and m.get("content")]
    def key(message):
        content = message["content"].strip().replace("\r\n", "\n")
        if message["role"] == "assistant":
            match = LABEL.match(content)
            if match:
                content = content[match.end():].lstrip()
                # Legacy records sometimes contain the same speaker label twice.
                duplicate = LABEL.match(content)
                if duplicate and duplicate.group(1) == match.group(1):
                    content = content[duplicate.end():].lstrip()
                content = match.group(1) + ":" + content
        return message["role"], content
    saved, live = clean(database), clean(browser)
    if not live:
        return recent_rounds(saved, limit)
    # Find the longest suffix of saved history already present in live history.
    # Two-message anchors avoid mistaking an isolated repeated 'hi' for overlap.
    best = None
    for size in range(min(len(saved), len(live)), 1, -1):
        target = [key(m) for m in saved[-size:]]
        for start in range(len(live)-size, -1, -1):
            if target == [key(m) for m in live[start:start+size]]:
                best = (size, start)
                break
        if best:
            break
    if best:
        size, start = best
        # Preserve whichever source reaches farther into the past.
        merged = saved[:-size] + live if start == 0 else live
    else:
        merged = saved + live
    return recent_rounds(merged, limit)
