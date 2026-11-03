"""Role identity, personal cross-channel experience and optional group participation."""
from contextlib import contextmanager
import hashlib
import re
from backend.app.infrastructure import storage
from backend.app.characters.role_routing import NAMES, reply_targets, select_speaker

ROLES = {'wan': '小婉', 'yan': '小研', 'liu': '小柳'}


def role_id(world, role):
    return hashlib.sha256((world + ':private:' + role).encode()).hexdigest()[:48]


@contextmanager
def role_scope(user, world, role):
    identity = role_id(world, role)
    storage.get_store().ensure(user, identity, {'world_id': world, 'channel': role,
        'title': ROLES[role], 'scene_state': {}, 'recent_log': []})
    token = storage.scope.set((user, identity))
    snapshot = storage.request_snapshot.set(storage.load_request_data(user, identity))
    try:
        yield identity
    finally:
        storage.request_snapshot.reset(snapshot)
        storage.scope.reset(token)


def choose(messages, scene, mode='natural'):
    named, group = reply_targets(messages)
    present = scene.get('在场人物', '')
    available = [name for name in NAMES if name in present] if present and present != '未知' else list(NAMES)
    # An explicit name is a deliberate address even when presence was omitted.
    if len(named) == 1 and not group:
        names = named
    elif group or len(named) > 1 or mode == 'all':
        names = [name for name in (named or list(NAMES)) if name in available]
    else:
        main = select_speaker(messages, []) or scene.get('主要交谈角色')
        names = [main] if main in available else available[:1]
    return [key for name in names for key, value in ROLES.items() if name == value]


def normalize_reply(text, name):
    """Discard accidentally generated lines for a different speaker."""
    result = []
    accepting = True
    for line in text.strip().splitlines():
        match = re.match(r'^\s*(小婉|小研|小柳)\s*[：:]\s*(.*)', line)
        if match:
            accepting = match[1] == name
            if accepting:
                result.append(match[2])
        elif accepting:
            result.append(line)
    body = '\n'.join(result).strip()
    if not body:
        raise ValueError('角色模型没有返回本人的有效回复')
    # Action brackets read better without a first-person subject; speech keeps it.
    body = re.sub(r'([（(])\s*我(?!们)', r'\1', body)
    return name + '：' + body


SILENCE = '[[SILENT]]'


def candidates(messages):
    """Ordering is a chance to speak, never an obligation or physical-presence filter."""
    named, _ = reply_targets(messages)
    preferred = [role for role, name in ROLES.items() if name in named]
    return preferred + [role for role in ROLES if role not in preferred]


def experience(user, world, role, limit=60):
    """One person's chronological experience across public and own private windows."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from backend.app.conversations import chat_history
    private = role_id(world, role)
    # Check ownership before accessing message rows; no other actor's private ID is queried.
    storage.get_store().read(user, world)
    storage.get_store().read(user, private)
    with Session(storage.get_store().engine) as db:
        rows=list(db.scalars(select(chat_history.Message).where(
            chat_history.Message.conversation_id.in_([world, private]))
            .order_by(chat_history.Message.created_at.desc(),chat_history.Message.id.desc()).limit(limit)))
        events=[];remaining=24000
        for row in rows:
            if remaining<=0: break
            text=row.content[:min(3000,remaining)]
            remaining-=len(text)
            events.append({'channel':'群聊' if row.conversation_id==world else '你与用户的私聊',
                           'time':row.created_at,'speaker':row.speaker or '用户','text':text})
        return list(reversed(events))
