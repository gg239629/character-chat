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

class SettingsIn(BaseModel):
    inherit_model: bool | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    persona: str | None = Field(default=None, max_length=4000)
    group_mode: str | None = Field(default=None, pattern=r'^(natural|all)$')
    relationship: str | None = Field(default=None, max_length=2000)
    public_persona: str | None = Field(default=None,max_length=1000)
    memory_preferences: dict[str,float] | None = None
    behavior: role_actions.Behavior | None = None
    adult_mode: bool | None = None

class HomeModelIn(BaseModel):
    model:str=Field(default='',max_length=200)
    base_url:str=Field(default='',max_length=2000)
    api_key:str=Field(default='',max_length=4000)
    temperature:float=Field(default=1,ge=0,le=2)
    max_tokens:int=Field(default=1500,ge=0,le=100000)
    embedding_model:str=Field(default='',max_length=200)

def home_model_draft(body):
    cfg=model_defaults.read(storage.scope.get()[0])
    cfg.update(model=body.model.strip(),base_url=body.base_url.strip(),temperature=body.temperature,
        max_tokens=body.max_tokens or None,timeout=120,embedding_model=body.embedding_model.strip())
    if body.api_key.strip():cfg['api_key']=body.api_key.strip()
    return cfg

@router.get('/api/home/model')
def home_model():
    import hashlib
    user=storage.scope.get()[0];cfg=model_defaults.read(user)
    return {'model':cfg.get('model',''),'base_url':cfg.get('base_url',''),
        'embedding_model':cfg.get('embedding_model',''),
        'temperature':cfg.get('temperature',1),'max_tokens':cfg.get('max_tokens',1500) or 0,
        'api_key_masked':services.models().mask_key(cfg.get('api_key','')),'configured':bool(cfg.get('model') and cfg.get('api_key')),
        'tutorial_scope':hashlib.sha256(user.encode()).hexdigest()[:16]}

@router.put('/api/home/model')
def save_home_model(body:HomeModelIn):
    cfg=services.models().home_model_draft(body)
    if services.models().check_cfg(cfg):raise HTTPException(422,'请填写模型名称和API Key')
    try:model_defaults.save(storage.scope.get()[0],cfg)
    except ValueError:raise HTTPException(400,'无法加密保存密钥，请检查服务加密配置')
    return services.models().home_model()

@router.post('/api/home/model/test')
async def test_home_model(body:HomeModelIn):
    cfg=services.models().home_model_draft(body)
    if services.models().check_cfg(cfg):raise HTTPException(422,'请填写模型名称和API Key')
    client=services.models().make_client(cfg)
    try:
        response=await model_usage.complete(client,cfg,[{'role':'user','content':'只回复：好'}],'connection_test',temperature=0,max_tokens=40)
        return {'ok':bool(response.choices),'model':cfg['model']}
    except Exception as error:raise HTTPException(502,'连接失败：'+services.models().friendly_error(error))
    finally:await client.close()

@router.get('/api/usage')
def usage_report(days:int=7):
    return model_usage.report(storage.scope.get()[0],max(1,min(30,days)))

@router.get("/api/settings")
def get_settings():
    """配置文件里的值 + 实际生效的值。api_key 只给掩码。"""
    file_cfg = services.models().load_config()
    eff = services.models().load_config()
    return {
        "model": file_cfg.get("model", ""),
        "base_url": file_cfg.get("base_url", ""),
        "temperature": file_cfg.get("temperature", 1.0),
        "max_tokens": file_cfg.get("max_tokens", 1500),
        "api_key_masked": services.models().mask_key(file_cfg.get("api_key", "") or eff.get("api_key", "")),
        "effective": {
            "model": eff.get("model", ""),
            "base_url": eff.get("base_url", ""),
            "has_key": bool(eff.get("api_key")),
        },
        "persona": storage.read_document('persona', ''),
        "relationship": storage.read_document('relationship', ''),
        "public_persona": storage.read_document('public_persona',''),
        "memory_preferences": storage.read_document('memory_preferences',{'group_public':.7,'private_public':.3}),
        "group_mode": storage.read_document('group_mode', 'natural'),
        "adult_mode": bool(services.api().read_story_flag('adult_mode', False)),
        "purpose": "scene" if not chat_world.get() or chat_world.get()['channel'] == 'group' else "character",
        "inherit_model":not storage.read_document('model_override',False) if model_defaults.read(storage.scope.get()[0]) else not bool(storage.read_document('settings',{})),
    }

@router.post("/api/settings")
def set_settings(body: SettingsIn):
    """写回 config/model.json。api_key 留空表示不改。"""
    if body.behavior is not None:
        raise HTTPException(422,'角色主动行为由性格与日常互动决定，不支持手动配置')
    if body.memory_preferences is not None:
        if set(body.memory_preferences)-{'group_public','private_public'} or any(not .15<=v<=.85 for v in body.memory_preferences.values()):
            raise HTTPException(422,'记忆预算比例需在0.15至0.85之间')
    cfg = dict(services.models().load_config())

    if body.model is not None:
        cfg["model"] = body.model.strip()
    if body.base_url is not None:
        cfg["base_url"] = body.base_url.strip()
    if body.temperature is not None:
        cfg["temperature"] = max(0.0, min(2.0, float(body.temperature)))
    if body.max_tokens is not None:
        cfg["max_tokens"] = None if int(body.max_tokens) <= 0 else int(body.max_tokens)
    if body.api_key is not None and body.api_key.strip():
        cfg["api_key"] = body.api_key.strip()

    try:
        encrypted = secrets_store.encode_settings({key: cfg.get(key) for key in ('model','base_url','api_key','temperature','max_tokens','timeout')})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    model_fields=('model','base_url','api_key','temperature','max_tokens')
    if body.inherit_model is True:
        storage.write_document('settings',{})
        storage.write_document('model_override',False)
    elif body.inherit_model is False or any(getattr(body,k) is not None for k in model_fields):
        storage.write_document('settings',encrypted)
        storage.write_document('model_override',True)
    if body.persona is not None:
        storage.write_document('persona', body.persona.strip())
    if body.relationship is not None:
        storage.write_document('relationship', body.relationship.strip())
    if body.public_persona is not None:
        storage.write_document('public_persona',body.public_persona.strip())
    if body.memory_preferences is not None:
        storage.write_document('memory_preferences',body.memory_preferences)
    if body.behavior is not None:
        storage.write_document('behavior',body.behavior.model_dump())
    if body.group_mode is not None:
        storage.write_document('group_mode', body.group_mode)
    if body.adult_mode is not None:
        services.api().write_story_flag('adult_mode', bool(body.adult_mode))
    return services.api().get_settings()

@router.post("/api/test")
async def test_model(body: SettingsIn | None = None):
    """Test draft settings without saving them; blank keys reuse the saved key."""
    cfg = dict(services.models().load_config())
    if body is not None and body.inherit_model is True:cfg=model_defaults.read(storage.scope.get()[0])
    if body is not None and body.inherit_model is not True:
        for key in ("model", "base_url", "api_key"):
            value = getattr(body, key)
            if value is not None and (key != "api_key" or value.strip()):
                cfg[key] = value.strip()
    problem = services.models().check_cfg(cfg)
    if problem:
        return JSONResponse({"error": problem}, 400)

    client = services.models().make_client(cfg)
    try:
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                resp = await model_usage.complete(client,cfg,[{"role":"user","content":"只回一个字：好"}],'connection_test',temperature=0)
                text = (resp.choices[0].message.content or "").strip()
                return {
                    "ok": True,
                    "model": cfg["model"],
                    "reply": text or "（连上了，但这个模型没返回文字，可能被它的思考过程用完了 token）",
                }
            except Exception as exc:
                last_exc = exc
                continue

        return JSONResponse({"error": services.models().friendly_error(last_exc)}, 502)
    finally:
        await client.close()
