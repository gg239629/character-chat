"""Process-local raw-message buffer; atomic minute checkpoints for messages and memory."""
import copy
import threading
import time
from datetime import datetime,timezone
from sqlalchemy import select,update
from sqlalchemy.orm import Session
from backend.app.infrastructure import storage

INTERVAL=60
_LOCK=threading.RLock()
_BUFFERS={}


def _key(user,conversation):return (storage.get_store().engine,user,conversation)


def pending(user,conversation):
    with _LOCK:return copy.deepcopy(_BUFFERS.get(_key(user,conversation),{}).get('messages',[]))


def checkpoint_due(user,conversation):
    with _LOCK:return _BUFFERS.get(_key(user,conversation),{}).get('due',0)


def message(user,conversation,identity):
    return next((item for item in pending(user,conversation) if item['id']==identity),None)


def append(turn,sequence,role,content,speaker='',mirror=True,metadata=None):
    from backend.app.conversations import chat_history
    user,conversation=storage.scope.get();store=storage.get_store()
    identity=chat_history.identity(conversation,turn,sequence)
    with _LOCK:
        data=store.read(user,conversation)
        if turn.startswith('import-') and data.get('display_cleared_at',0):return False
        key=_key(user,conversation)
        with Session(store.engine) as db:
            if db.get(chat_history.Message,identity):return False
            last=db.scalar(select(chat_history.Message.created_at).where(chat_history.Message.conversation_id==conversation).order_by(chat_history.Message.created_at.desc()).limit(1)) or 0
        buffer=_BUFFERS.setdefault(key,{'messages':[],'due':(int(time.time()//60)+1)*60})
        if any(item['id']==identity for item in buffer['messages']):return False
        last=max([last]+[item['created_at'] for item in buffer['messages']])
        stamp=max(time.time(),last+.000001,data.get('display_cleared_at',0)+.000001)
        record=None
        if mirror:
            from backend.app.conversations import message_service
            record=message_service.memory_record(identity,conversation,data,role,content,speaker,stamp,metadata)
        buffer['messages'].append({'id':identity,'role':role,'content':content,'speaker':speaker,'created_at':stamp,'record':record})
        from backend.app.infrastructure import events
        events.publish(user)
        return True


def effective(data,user=None,conversation=None):
    user,conversation=(user,conversation) if user is not None else storage.scope.get()
    records=[item['record'] for item in pending(user,conversation) if item.get('record')]
    records += data.get('memory_inbox',[]) # Recover transitional checkpoints from the previous version.
    if not records:return data
    view=copy.deepcopy(data);known={item.get('id') for item in view.get('recent_log',[])}
    view.setdefault('recent_log',[]).extend(record for record in records if record['id'] not in known)
    return view


def link(user,conversation,source,action):
    with _LOCK:
        for item in _BUFFERS.get(_key(user,conversation),{}).get('messages',[]):
            record=item.get('record')
            if record and record['id']==source:
                record.setdefault('action_id',action)
                record['action_ids']=list(dict.fromkeys(record.get('action_ids',[])+[action]))


def flush(user,conversation,now=None,force=False):
    from backend.app.conversations import chat_history
    now=time.time() if now is None else now;store=storage.get_store()
    with _LOCK:
        key=_key(user,conversation);buffer=_BUFFERS.get(key)
        existing=store.read(user,conversation)
        legacy=bool(existing.get('memory_inbox') and (force or existing.get('memory_save_due',0)<=now))
        if not legacy and (not buffer or (not force and buffer['due']>now)):return False
        batch=copy.deepcopy(buffer['messages']) if buffer and (force or buffer['due']<=now) else []
        for _ in range(10):
            with Session(store.engine) as db:
                row=db.get(storage.Conversation,conversation)
                if not row or row.user_id!=user:raise PermissionError('Conversation unavailable')
                data=copy.deepcopy(row.data);storage.prepare_policy(data)
                records=list(data.get('memory_inbox',[])) if legacy else []
                for item in batch:
                    if not db.get(chat_history.Message,item['id']):
                        db.add(chat_history.Message(**{key:item[key] for key in ['id','role','content','speaker','created_at']},conversation_id=conversation))
                    if item.get('record'):records.append(item['record'])
                known={item.get('id') for item in data.get('recent_log',[])}
                data.setdefault('recent_log',[]).extend(record for record in records if record['id'] not in known)
                data['memory_inbox']=[];data['memory_save_due']=0;data['memory_saved_at']=now
                storage.trim_recent(data)
                if not data.get('world_id'):
                    from backend.app.characters.role_routing import recent_rounds
                    data['public_history']=recent_rounds([{'role':'user' if r.get('who') in storage.USER_NAMES else 'assistant',
                        'content':r.get('text','') if r.get('who') in storage.USER_NAMES else r.get('who','')+'：'+r.get('text','')} for r in data.get('recent_log',[])],10)
                result=db.execute(update(storage.Conversation).where(storage.Conversation.id==conversation,storage.Conversation.version==row.version).values(data=data,version=row.version+1))
                if not result.rowcount:db.rollback();continue
                db.commit()
                if batch:_BUFFERS.pop(key,None)
                cached=storage.request_snapshot.get()
                if cached and cached['scope']==(user,conversation):cached['data']=data
                return True
        raise RuntimeError('Batch save conflicted; buffer retained')


def flush_due(now=None,force=False):
    now=time.time() if now is None else now;engine=storage.get_store().engine
    with _LOCK:keys=[(user,conversation) for (db,user,conversation),buffer in _BUFFERS.items() if db is engine and (force or buffer['due']<=now)]
    with Session(engine) as db:
        keys+=list(db.execute(select(storage.Conversation.user_id,storage.Conversation.id).where(storage.Conversation.data['memory_save_due'].as_float()>0,
            storage.Conversation.data['memory_save_due'].as_float()<=now)))
    for user,conversation in set(keys):
        try:
            if flush(user,conversation,now,force):
                data=storage.get_store().read(user,conversation)
                if data.get('pending_archive'):
                    from backend.app.infrastructure import maintenance
                    maintenance.enqueue(user,data.get('world_id',conversation),data.get('channel','group') if data.get('world_id') else 'group','minute-checkpoint',now,scene=False,archive_now=True)
        except Exception:continue


def discard(user,conversations):
    with _LOCK:
        for conversation in conversations:_BUFFERS.pop(_key(user,conversation),None)


def all_pending():
    engine=storage.get_store().engine
    with _LOCK:return [(user,conversation,copy.deepcopy(buffer['messages'])) for (db,user,conversation),buffer in _BUFFERS.items() if db is engine]
