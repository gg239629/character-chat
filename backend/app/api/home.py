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

class HomeSettingsIn(BaseModel):
    initiative_enabled: bool

@router.get('/api/home/settings')
def home_settings():
    from backend.app.runtime import initiative_control
    return {'initiative_enabled':initiative_control.enabled(storage.scope.get()[0],chat_world.get()['id'])}

@router.get('/api/home/task-health')
def background_health():
    return {'failures':task_health.report(storage.scope.get()[0],chat_world.get()['id'])}

@router.post('/api/home/tasks/{stage}/{identity}/retry')
def retry_background_task(stage:str,identity:str):
    try:return task_health.retry(storage.scope.get()[0],chat_world.get()['id'],stage,identity)
    except PermissionError:raise HTTPException(404,'Task unavailable')
    except ValueError as error:raise HTTPException(409,str(error))

@router.put('/api/home/settings')
def update_home_settings(body: HomeSettingsIn):
    from backend.app.runtime import initiative_control
    value=initiative_control.set_enabled(storage.scope.get()[0],chat_world.get()['id'],body.initiative_enabled)
    return {'initiative_enabled':value}

class ResetStoryIn(BaseModel):
    confirm: bool = False

@router.post('/api/home/reset')
def reset_from_home(body: ResetStoryIn):
    if not body.confirm:raise HTTPException(422,'请确认删除聊天记录与记忆')
    from backend.app.world import story_reset
    try:
        return story_reset.reset_story(storage.scope.get()[0],chat_world.get()['id'],backup=False)
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))
