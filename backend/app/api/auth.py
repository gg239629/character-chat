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

class AccountIn(BaseModel):
    username: str = Field(min_length=1,max_length=40)
    password: str = Field(min_length=1,max_length=128)
    invite_code: str = Field(default='',max_length=256)
    endpoint: str = Field(default='',max_length=3000)

@router.get('/api/auth/status')
def account_status(request: Request):
    user=accounts.identify(request.cookies.get('chat_session','')) if accounts.enabled() else 'owner'
    return JSONResponse({'enabled':accounts.enabled(),'logged_in':bool(user),
        'username':accounts.account_name(user) if user and accounts.enabled() else '',
        'scope':__import__('hashlib').sha256(user.encode()).hexdigest()[:24] if user else '',
        'registration_mode':accounts.registration_mode()},headers={'Cache-Control':'no-store'})

@router.post('/api/auth/register')
def register_account(body: AccountIn,request: Request):
    if not accounts.enabled():raise HTTPException(404,'账号功能尚未启用')
    services.api().auth_rate_limit(request)
    try:
        accounts.check_invite(body.invite_code)
        accounts.register(body.username,body.password)
    except ValueError as error:raise HTTPException(422,str(error))
    return {'ok':True}

@router.post('/api/auth/login')
def login_account(body: AccountIn,request: Request):
    if not accounts.enabled():raise HTTPException(404,'账号功能尚未启用')
    services.api().auth_rate_limit(request)
    if not accounts.allow_attempt('name:'+body.username.strip().lower(),10):raise HTTPException(429,'尝试次数过多，请一分钟后重试')
    try:token=accounts.login(body.username,body.password,request.cookies.get('chat_session',''),body.endpoint)
    except ValueError as error:raise HTTPException(401,str(error))
    response=JSONResponse({'ok':True})
    response.set_cookie('chat_session',token,httponly=True,secure=request.url.scheme=='https',samesite='strict',max_age=7*86400)
    return response

def auth_rate_limit(request):
    if not accounts.allow_attempt('ip:'+str(request.client.host if request.client else 'unknown'),30):
        raise HTTPException(429,'操作过于频繁，请一分钟后重试')

class LogoutIn(BaseModel):
    endpoint: str = Field(default='',max_length=3000)

class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1,max_length=128)
    new_password: str = Field(min_length=10,max_length=128)
    endpoint: str = Field(default='',max_length=3000)

@router.post('/api/auth/password')
def change_account_password(body: PasswordIn,request: Request):
    if not accounts.enabled():raise HTTPException(404,'账号功能尚未启用')
    user=accounts.identify(request.cookies.get('chat_session',''))
    if not user:raise HTTPException(401,'请先登录')
    services.api().auth_rate_limit(request)
    try:accounts.change_password(user,body.current_password,body.new_password,body.endpoint)
    except ValueError as error:raise HTTPException(422,str(error))
    response=JSONResponse({'ok':True});response.delete_cookie('chat_session');return response

@router.post('/api/auth/logout')
def logout_account(request: Request,body: LogoutIn | None = None):
    accounts.logout(request.cookies.get('chat_session',''),body.endpoint if body else None)
    response=JSONResponse({'ok':True});response.delete_cookie('chat_session');return response
