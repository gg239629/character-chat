"""Explicit, backed-up fresh start of one user's four-channel story."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import uuid4
from sqlalchemy import select, delete, update
from sqlalchemy.orm import Session
import actors
import chat_history
import maintenance
import push_notifications
import role_actions
import storage
import peer_chat
import chat_inbox

CONFIG_FIELDS={'title','avatar','settings','persona','behavior','group_mode','world_id','channel','public_persona','memory_preferences','initiative_enabled','user_profile'}


def reset_story(user,world,backup_dir=None,backup=True):
    store=storage.get_store()
    root=store.read(user,world)
    if root.get('world_id'):raise ValueError('Reset the root story, not a private channel')
    ids=[world]+[actors.role_id(world,role) for role in actors.ROLES]
    for role in actors.ROLES:
        store.ensure(user,actors.role_id(world,role),{'world_id':world,'channel':role,'title':actors.ROLES[role]})
    import memory_autosave
    for conversation in ids:memory_autosave.flush(user,conversation,force=True)
    now=time.time()
    filename=None
    if backup:
        backup_dir=Path(backup_dir);backup_dir.mkdir(parents=True,exist_ok=True)
        filename=backup_dir/('story-reset-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.json')
    models=[(storage.Conversation,storage.Conversation.id.in_(ids)),
        (chat_history.Message,chat_history.Message.conversation_id.in_(ids)),
        (chat_history.ReadCursor,chat_history.ReadCursor.conversation_id.in_(ids)),
        (chat_history.Lease,chat_history.Lease.conversation_id.in_(ids)),
        (maintenance.Job,(maintenance.Job.user_id==user)&(maintenance.Job.world_id==world)),
        (role_actions.Action,(role_actions.Action.user_id==user)&(role_actions.Action.world_id==world)),
        (push_notifications.PushJob,(push_notifications.PushJob.user_id==user)&(push_notifications.PushJob.world_id==world)),
        (role_actions.Checkpoint,role_actions.Checkpoint.id.in_(ids[1:])),
        *[(model,(model.user_id==user)&(model.world_id==world)) for model in
            (peer_chat.Message,peer_chat.Archive,peer_chat.Job,chat_inbox.Turn)]]
    with Session(store.engine) as db:
        active=db.scalar(select(chat_history.Lease.token).where(chat_history.Lease.conversation_id.in_(ids),chat_history.Lease.expires>now).limit(1))
        delivering=db.scalar(select(role_actions.Action.id).where(role_actions.Action.user_id==user,role_actions.Action.world_id==world,
            role_actions.Action.status=='sending',role_actions.Action.lease_until>now).limit(1))
        peer_running=db.scalar(select(peer_chat.Job.id).where(peer_chat.Job.user_id==user,peer_chat.Job.world_id==world,
            peer_chat.Job.status=='running',peer_chat.Job.lease_until>now).limit(1))
        maintenance_running=db.scalar(select(maintenance.Job.id).where(maintenance.Job.user_id==user,maintenance.Job.world_id==world,
            maintenance.Job.status=='running',maintenance.Job.lease_until>now).limit(1))
        inbox_running=db.scalar(select(chat_inbox.Turn.id).where(chat_inbox.Turn.user_id==user,chat_inbox.Turn.world_id==world,
            chat_inbox.Turn.status=='running',chat_inbox.Turn.lease_until>now).limit(1))
        if active or delivering or peer_running or maintenance_running or inbox_running:
            raise RuntimeError('还有正在处理的消息，请稍等片刻再重置')
        rows=list(db.scalars(select(storage.Conversation).where(storage.Conversation.id.in_(ids))))
        if len(rows)!=4 or any(row.user_id!=user for row in rows):raise PermissionError('Story ownership mismatch')
        if filename:
            snapshot={'format':1,'user':user,'world':world,'created_at':datetime.now(timezone.utc).isoformat(),'tables':{}}
            for model,condition in models:
                records=list(db.scalars(select(model).where(condition)))
                snapshot['tables'][model.__tablename__]=[{column.name:getattr(row,column.name) for column in model.__table__.columns} for row in records]
            with filename.open('x',encoding='utf-8') as output:
                json.dump(snapshot,output,ensure_ascii=False,indent=2)
        seed='卧室·刚起床'
        for row in rows:
            data={key:value for key,value in row.data.items() if key in CONFIG_FIELDS}
            data.update(memory_policy=2,messages_initialized=True,recent_log=[],pending_archive=[],long_summaries=[],important_facts=[],
                original_backups=[],display_cleared_at=now,memory_reset_at=now)
            if row.id==world:
                data.update(scene_state={'时间':'第一天早晨08:00','地点':'各自住处','在场人物':'各自活动',
                    **{name:seed for name in actors.ROLES.values()},'主要交谈角色':'未知','上一段停在':'新一天早晨，刚起床'},
                    role_states={role:{'status':seed,'source':'reset','updated_at':datetime.now(timezone.utc).isoformat()} for role in actors.ROLES},
                    public_history=[],scene_applied_at=now,story_clock={'day':1,'minute':480},world_clock_revision=now,personal_progress={})
            updated=db.execute(update(storage.Conversation).where(storage.Conversation.id==row.id,storage.Conversation.version==row.version)
                .values(data=data,version=row.version+1))
            if not updated.rowcount:raise RuntimeError('重置期间故事发生变化，操作已回滚')
        for model,condition in models[1:]:db.execute(delete(model).where(condition))
        db.commit()
    memory_autosave.discard(user,ids)
    snapshot_cache=storage.request_snapshot.get()
    if snapshot_cache and snapshot_cache['scope'][0]==user and snapshot_cache['scope'][1] in ids:
        snapshot_cache['data']=store.read(*snapshot_cache['scope'])
    return {'backup':str(filename.resolve()) if filename else None,'channels':4,'start':'第一天早晨08:00','status':seed}
