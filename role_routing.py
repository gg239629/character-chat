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
