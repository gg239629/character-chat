"""Account-scoped, content-free invalidations. Database remains the source of truth."""
import asyncio
import threading
from contextlib import asynccontextmanager
from sqlalchemy import event
from sqlalchemy.orm import Session

_lock=threading.RLock()
_listeners={}
_installed=False


def publish(user):
    with _lock:targets=list(_listeners.get(user,()))
    for loop,queue in targets:
        def deliver(queue=queue):
            if queue.empty():queue.put_nowait(True)
        try:loop.call_soon_threadsafe(deliver)
        except RuntimeError:pass


@asynccontextmanager
async def subscribe(user):
    pair=(asyncio.get_running_loop(),asyncio.Queue(maxsize=1))
    with _lock:_listeners.setdefault(user,set()).add(pair)
    try:yield pair[1]
    finally:
        with _lock:
            _listeners.get(user,set()).discard(pair)
            if not _listeners.get(user):_listeners.pop(user,None)


def install():
    global _installed
    if _installed:return
    _installed=True
    @event.listens_for(Session,'before_flush')
    def collect(db,*args):
        from backend.app.infrastructure.storage import Conversation
        users=db.info.setdefault('event_users',set())
        for row in set(db.new)|set(db.dirty)|set(db.deleted):
            if getattr(row,'__tablename__','') not in ('chat_conversations','chat_messages','chat_read_cursors','chat_inbox_turns'):continue
            user=getattr(row,'user_id',None)
            cid=getattr(row,'conversation_id',None)
            if user is None and cid:
                with db.no_autoflush:parent=db.get(Conversation,cid)
                if parent:user=parent.user_id
            if user:users.add(user)
    @event.listens_for(Session,'after_commit')
    def committed(db):
        for user in db.info.pop('event_users',set()):publish(user)
    @event.listens_for(Session,'after_rollback')
    def rollback(db):db.info.pop('event_users',None)
