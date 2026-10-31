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

def select_speaker(messages, memory_messages):
    current = last_speaker(messages)
    if current:
        return current
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content") or ""
        mentioned = [name for name in NAMES if name in content]
        if len(mentioned) == 1:
            return mentioned[0]
    return last_speaker(messages) or last_speaker(memory_messages)


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
