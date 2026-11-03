"""Chat API: scoped conversations, independent character agents and background maintenance.
Fixed chat style is in prompt/system.md; actor and channel rules live in actor_instructions.py.
Runtime identity, history, scene evidence and model settings are loaded from scoped storage.
Start with: python -m uvicorn server:app --host 127.0.0.1 --port 8788
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from backend.app.infrastructure import model_usage
from backend.app.characters import model_defaults
from backend.app.world import world_events
from backend.app.conversations import peer_chat
from backend.app.conversations import chat_inbox
from backend.app.infrastructure import task_health
from backend.app.accounts import accounts
from backend.app.conversations import message_service

ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = ROOT / "config" / "model.json"
PROMPT_FILE = ROOT / "prompt" / "system.md"
INJECT_FILE = ROOT / "prompt" / "inject.zh.md"
MEMORY_DIR = ROOT / "memory"
MEMORY_MD = MEMORY_DIR / "陪伴角色.md"
MEMORY_JSON = MEMORY_DIR / "记忆包.json"

from backend.app.characters.role_routing import recent_rounds
from backend.app.memory import long_memory
from backend.app.infrastructure import storage
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.infrastructure import secrets_store
from backend.app.runtime import role_actions
from backend.app.conversations import push_notifications
from backend.app.infrastructure import maintenance
from backend.app.world import scene_labels
from backend.app.memory import role_memory
from backend.app.runtime import group_coordinator
from backend.app.world import world_time
from fastapi import Request
from uuid import uuid4
from contextvars import ContextVar

from backend.app.core.context import chat_world, scene_revision
PRIVATE_NAMES = {"wan": "小婉", "yan": "小研", "liu": "小柳"}

from fastapi import APIRouter
from backend.app.core import services
from backend.app.core.schemas import ChatIn

router=APIRouter()

@router.get("/api/conversations")
def list_conversations():
    stories=storage.get_store().conversations(storage.scope.get()[0])
    summaries=chat_history.summaries()
    for story in stories:
        story['activity']={'group':summaries.get(story['id'],{})}
        for role in services.world().PRIVATE_NAMES:
            story['activity'][role]=summaries.get(actors.role_id(story['id'],role),{})
    return stories

class ReadIn(BaseModel):
    message_id: str = Field(max_length=64)

@router.post('/api/messages/read')
def read_messages(body: ReadIn):
    try:
        chat_history.mark_read(body.message_id)
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))
    return {'ok':True}

@router.get('/api/actions')
def list_role_actions():
    world=chat_world.get()
    actor=world['channel'] if world['channel'] in services.world().PRIVATE_NAMES else None
    return {'actions':role_actions.audit(storage.scope.get()[0],world['id'],actor)}

@router.post('/api/actions/{identity}/cancel')
def cancel_role_action(identity: str):
    world=chat_world.get()
    if world['channel'] in services.world().PRIVATE_NAMES:
        available={item['id'] for item in role_actions.audit(storage.scope.get()[0],world['id'],world['channel'])}
        if identity not in available:
            raise HTTPException(404,'找不到当前角色的行动')
    if not role_actions.cancel(storage.scope.get()[0],world['id'],identity):
        raise HTTPException(409,'此行动已执行或无法取消')
    return {'ok':True}

@router.post('/api/actions/check')
async def check_role_intentions():
    world=chat_world.get()
    if world['channel'] not in services.world().PRIVATE_NAMES:
        raise HTTPException(400,'请在角色私聊中检查主动意图')
    result=await role_actions.autonomous(storage.scope.get()[0],world['id'],world['channel'])
    if result and result.get('target') in services.world().PRIVATE_NAMES:
        result=None  # Internal contact must not expose a hidden message through this API.
    return {'action':result,'message':'已检查；未满足条件或角色没有意愿时不会发消息。'}

@router.post('/api/jobs/tick')
async def external_role_tick(request: Request):
    import secrets
    token=os.getenv('ROLE_CRON_TOKEN','')
    supplied=request.headers.get('Authorization','')
    if len(token)<32 or not secrets.compare_digest(supplied,'Bearer '+token):
        raise HTTPException(401,'Scheduler authentication required')
    await role_actions.tick()
    return {'ok':True}

@router.get('/api/push/config')
def push_config():
    try:
        pair=push_notifications.keys()
        return {'public_key':pair[1] if pair else None}
    except Exception:
        return {'public_key':None}

@router.post('/api/push/subscribe')
def push_subscribe(body: dict):
    try:
        identity=push_notifications.subscribe(storage.scope.get()[0],body)
    except (ValueError,PermissionError) as exc:
        raise HTTPException(422,str(exc))
    return {'ok':True,'id':identity}

@router.post('/api/push/unsubscribe')
def push_unsubscribe(body: dict):
    endpoint=body.get('endpoint','')
    if not isinstance(endpoint,str) or len(endpoint)>3000:
        raise HTTPException(422,'Invalid endpoint')
    push_notifications.unsubscribe(storage.scope.get()[0],endpoint)
    return {'ok':True}

@router.get("/api/memory/export")
def export_memory():
    pack = storage.memory_pack()
    pack["scene_state"] = services.world().displayed_scene()
    return pack

class ConversationProfileIn(BaseModel):
    title: str
    avatar: str = ""

@router.get("/api/conversation/profile")
def conversation_profile():
    data = storage.current_data()
    identity = storage.scope.get()[1]
    from backend.app.characters import avatar_assets
    return {"id": identity, "title": data.get("title") or ("原有故事" if identity == "default" else "未命名故事"), "avatar": data.get("avatar", ""),
        "display_avatar":avatar_assets.display(data.get('channel','group'),data.get('avatar','')),
        "default_avatar":avatar_assets.DEFAULTS.get(data.get('channel','group'),'')}

@router.put("/api/conversation/profile")
def update_conversation_profile(body: ConversationProfileIn):
    import base64
    title = body.title.strip()
    if not title or len(title) > 60:
        raise HTTPException(422, "会话名称应为 1–60 个字")
    from backend.app.characters import avatar_assets
    if body.avatar and body.avatar not in avatar_assets.DEFAULTS.values():
        try:
            prefix, encoded = body.avatar.split(",", 1)
            if prefix not in ("data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64") or len(encoded) > 400000:
                raise ValueError()
            raw = base64.b64decode(encoded, validate=True)
            valid = ((prefix == "data:image/jpeg;base64" and raw.startswith(b"\xff\xd8\xff")) or
                     (prefix == "data:image/png;base64" and raw.startswith(b"\x89PNG\r\n\x1a\n")) or
                     (prefix == "data:image/webp;base64" and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))
            if not valid:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "请上传有效的 PNG、JPEG 或 WebP 小尺寸头像")
    storage.get_store().mutate(*storage.scope.get(), lambda data: data.update(title=title, avatar=body.avatar))
    return services.api().conversation_profile()

@router.get("/api/messages")
def get_messages(after: str = '', incremental: bool = False):
    chat_history.bootstrap()
    from backend.app.memory import memory_autosave
    user,conversation=storage.scope.get()
    due=memory_autosave.checkpoint_due(user,conversation)
    if incremental:messages,unchanged,delta=chat_history.updates(after)
    else:
        messages,unchanged=chat_history.poll_visible(after);delta=False
    return {"messages": [] if unchanged else messages, "unchanged":unchanged,"delta":delta,"auto_saved": not bool(due),
            "save_pending": bool(due), "next_save_at": due,
            "display_cleared_at": storage.read_document("display_cleared_at",0)}

@router.post('/api/messages/clear-display')
def clear_message_display():
    chat_history.bootstrap()
    cutoff=chat_history.clear_display()
    return {'ok':True,'messages':[],'display_cleared_at':cutoff}

@router.post("/api/messages/import")
def import_messages(body: ChatIn):
    if len(body.messages) > 200 or any(len(m.get('content', '')) > 100000 or m.get('role') not in ('user','assistant') for m in body.messages):
        raise HTTPException(422, '浏览器历史格式无效或过长')
    chat_history.bootstrap()
    return {"imported": chat_history.import_browser(body.messages), "messages": chat_history.visible_messages()}

@router.post('/api/send')
async def accept_message(body: ChatIn):
    try:
        from backend.app.characters.service import characters
        world=chat_world.get()
        text=next((m.get('content','') for m in reversed(body.messages) if m.get('role')=='user'),'')
        return await asyncio.to_thread(characters.send,storage.scope.get()[0],world['id'],world['channel'],text,body.request_id)
    except ValueError as exc:
        return JSONResponse({'error':str(exc)},422)

@router.get('/api/send/{identity}')
def accepted_message_state(identity: str):
    try:return chat_inbox.state(identity)
    except PermissionError:raise HTTPException(404,'Message unavailable')

@router.post('/api/send/{identity}/retry')
def retry_accepted_message(identity: str):
    try:return chat_inbox.retry(identity)
    except PermissionError:raise HTTPException(404,'Message unavailable')
    except ValueError as error:raise HTTPException(409,str(error))

@router.post("/api/save")
def save_memory(body: ChatIn):
    # Compatibility with older browsers: idempotently import, never append twice.
    chat_history.bootstrap()
    saved = chat_history.import_browser(body.messages)
    return {"saved":saved,"total":len(storage.memory_pack()["recent_log"]),
            "last_saved":datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
