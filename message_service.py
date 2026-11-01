"""Shared channel policy and transactional message writes for every visible channel."""
import copy
import time
from datetime import datetime, timezone
from sqlalchemy import update, select
from sqlalchemy.orm import Session
import storage
import actors


def memory_record(identity,cid,data,role,content,speaker,stamp,metadata=None):
    """One provenance format for buffered replies, inbox and action deliveries."""
    channel=data.get('channel','group') if data.get('world_id') else 'group'
    text=content[len(speaker)+1:] if speaker and content.startswith(speaker+'：') else content
    record={'id':identity,'who':speaker or '他','text':text,'saved_at':datetime.fromtimestamp(stamp,timezone.utc).isoformat(),
        'source':'direct','visibility':'public' if channel=='group' else 'private','conversation_id':cid,
        'channel':channel,'actor_id':None if channel=='group' else channel,
        'audience':['user',*actors.ROLES] if channel=='group' else ['user',channel]}
    if metadata:record.update({k:v for k,v in metadata.items() if k in ('source','action_id','audience')})
    return record


def conversation(user,world,channel):
    import channel_policy
    policy=channel_policy.resolve(user,world,channel)
    if not policy.user_visible:raise PermissionError('Hidden channel is not a user conversation')
    if channel=='group':return world
    if channel not in actors.ROLES:raise ValueError('Invalid channel')
    identity=actors.role_id(world,channel)
    storage.get_store().ensure(user,identity,{'world_id':world,'channel':channel,'title':actors.ROLES[channel],
        'scene_state':{},'recent_log':[]})
    return identity


def write(db,user,cid,turn,sequence,role,content,speaker='',metadata=None):
    """Caller owns commit: raw message, recent memory and work can be atomic."""
    import chat_history
    identity=chat_history.identity(cid,turn,sequence)
    row=db.get(storage.Conversation,cid)
    if not row or row.user_id!=user:raise PermissionError('Conversation unavailable')
    existing=db.get(chat_history.Message,identity)
    if existing:
        if existing.content!=content or existing.role!=role:raise ValueError('Message identity conflict')
        return existing,False
    data=copy.deepcopy(row.data);storage.prepare_policy(data)
    last=db.scalar(select(chat_history.Message.created_at).where(chat_history.Message.conversation_id==cid)
        .order_by(chat_history.Message.created_at.desc()).limit(1)) or 0
    stamp=max(time.time(),last+.000001,data.get('display_cleared_at',0)+.000001)
    message=chat_history.Message(id=identity,conversation_id=cid,role=role,content=content,speaker=speaker,created_at=stamp)
    db.add(message)
    record=memory_record(identity,cid,data,role,content,speaker,stamp,metadata)
    data.setdefault('recent_log',[]).append(record);storage.trim_recent(data)
    result=db.execute(update(storage.Conversation).where(storage.Conversation.id==cid,
        storage.Conversation.version==row.version).values(data=data,version=row.version+1))
    if not result.rowcount:raise RuntimeError('Concurrent message write; retry')
    return message,True


def durable_append(turn,sequence,role,content,speaker='',mirror=True,metadata=None,commit_hook=None):
    import memory_autosave
    user,cid=storage.scope.get()
    memory_autosave.flush(user,cid,force=True)
    for attempt in range(5):
        try:
            with Session(storage.get_store().engine) as db:
                message,inserted=write(db,user,cid,turn,sequence,role,content,speaker,metadata)
                if commit_hook:commit_hook(db,message.id,message.created_at)
                db.commit()
            cached=storage.request_snapshot.get()
            if cached and cached['scope']==(user,cid):cached['data']=storage.get_store().read(user,cid)
            return inserted
        except RuntimeError:
            if attempt==4:raise


def interruption_kind(text):
    """Explicit changes cancel unsent drafts; ordinary additions join the next reply."""
    import re
    return 'replace' if re.search(r'先别|别说了|停一下|停止|不用回|换个话题|改聊|改成|不是.{0,20}是|说错了|不对[，,。！!]',text) else 'append'


def cancels_draft(messages,after):
    return any(m['role']=='user' and m['created_at']>after and interruption_kind(m['content'])=='replace' for m in messages)


def deliver(user,world,sender,target,content,identity,event_id=''):
    """Single action transport. Storage adapters preserve each channel's privacy."""
    if sender not in actors.ROLES:raise ValueError('Invalid sender')
    import channel_policy
    channel='peer:'+'-'.join(sorted((sender,target))) if target in actors.ROLES else sender if target=='user' else target
    channel_policy.resolve(user,world,channel,sender)
    def receipt(db,message_id,stamp):
        if not event_id:return
        import role_actions
        action=db.get(role_actions.Action,event_id)
        if not action or (action.user_id,action.world_id,action.actor,action.target)!=(user,world,sender,target):
            raise PermissionError('Invalid action receipt')
        if action.status not in ('sending','sent'):raise RuntimeError('Action is no longer deliverable')
        action.status='sent';action.lease_until=0
        action.detail={**action.detail,'message_id':message_id,'sent_at':stamp}
    if target in actors.ROLES:
        import peer_chat
        result=peer_chat.send(user,world,sender,target,content,identity,event_id=event_id,commit_hook=receipt)
        return {'id':result['id'],'inserted':True,'channel':'peer','audience':[sender,target]}
    import role_actions
    import chat_history
    channel=sender if target=='user' else target
    with role_actions.window(user,world,channel):
        turn='action-'+identity[:48]
        claimed=chat_history.claim(turn)
        try:
            chat_history.bootstrap()
            inserted=durable_append(turn,0,'assistant',content,actors.ROLES[sender],metadata={
                'source':'role_action','action_id':event_id,'audience':['user',*actors.ROLES] if target=='group' else ['user',sender]},commit_hook=receipt)
            return {'id':chat_history.identity(storage.scope.get()[1],turn,0),'inserted':inserted,
                'channel':channel,'audience':['user',*actors.ROLES] if target=='group' else ['user',sender]}
        finally:
            if claimed:chat_history.release(turn)
