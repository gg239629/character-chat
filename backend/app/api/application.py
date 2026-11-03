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

app = FastAPI(title="陪伴角色 · 网页版")

@app.on_event("startup")
def initialize_database():
    storage.initialize()
    accounts.load_policy()
    secrets_store.migrate_credentials()
    peer_chat.migrate_legacy()


@app.on_event('startup')
async def start_maintenance():
    if os.getenv('RUN_MAINTENANCE_WORKER','true').lower() not in ('true','1','yes'):
        return
    worker=maintenance.Worker()
    app.state.maintenance_worker=worker
    worker.start()


@app.on_event('shutdown')
async def stop_maintenance():
    worker=getattr(app.state,'maintenance_worker',None)
    if worker:
        await worker.stop()
        app.state.maintenance_worker=None
    else:
        from backend.app.memory import memory_autosave
        await asyncio.to_thread(memory_autosave.flush_due,force=True)
    from backend.app.infrastructure import model_usage
    await asyncio.to_thread(model_usage.flush,force=True)


@app.on_event('startup')
async def start_role_scheduler():
    if os.getenv('RUN_ROLE_SCHEDULER','true').lower() not in ('true','1','yes'):
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler=AsyncIOScheduler()
    scheduler.add_job(role_actions.tick,'interval',seconds=60,max_instances=1,coalesce=True,
        misfire_grace_time=30,id='role-actions')
    scheduler.add_job(peer_chat.run_tick,'interval',seconds=5,max_instances=1,coalesce=True,
        misfire_grace_time=10,id='peer-chat')
    scheduler.add_job(chat_inbox.run,'interval',seconds=1,max_instances=1,coalesce=True,
        misfire_grace_time=10,id='chat-inbox')
    scheduler.start()
    app.state.role_scheduler=scheduler


@app.on_event('shutdown')
async def stop_role_scheduler():
    scheduler=getattr(app.state,'role_scheduler',None)
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

@app.middleware("http")
async def bind_conversation(request: Request, call_next):
    # Future authentication must set request.state.user_id after verification.
    # Never trust a user ID supplied by the browser.
    if request.method not in ('GET','HEAD','OPTIONS') and request.url.path.startswith('/api/') and request.url.path!='/api/jobs/tick':
        origin=request.headers.get('origin')
        expected=os.getenv('PUBLIC_ORIGIN','').rstrip('/') or str(request.base_url).rstrip('/')
        if request.headers.get('sec-fetch-site')=='cross-site' or (origin and origin.rstrip('/')!=expected):
            return JSONResponse({'error':'请求来源无效，请从网站页面操作'},403)
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path=='/api/jobs/tick':
        return await call_next(request)
    if request.url.path.startswith('/api/auth/'):
        return await call_next(request)
    if accounts.enabled():
        identity=await asyncio.to_thread(accounts.identify,request.cookies.get('chat_session',''))
        if not identity:return JSONResponse({'error':'请先登录'},401)
        supplied=request.headers.get('X-Session-Scope')
        if supplied and supplied!=__import__('hashlib').sha256(identity.encode()).hexdigest()[:24]:
            return JSONResponse({'error':'账号已切换，请重新打开页面','code':'account_changed'},409)
        request.state.user_id=identity
    user = getattr(request.state, "user_id", "owner")
    conversation = request.headers.get("X-Conversation-ID", "default")
    if accounts.enabled() and conversation=='default':conversation=accounts.root_id(user)
    channel = request.headers.get("X-Chat-Channel", "group")
    if channel not in ("group", "wan", "yan", "liu"):
        return JSONResponse({"error": "Invalid chat channel"}, 400)
    if len(conversation) > 64:
        return JSONResponse({"error": "Invalid conversation"}, 400)
    try:
        snapshot = await asyncio.to_thread(storage.load_request_data, user, conversation)
        if snapshot['data'].get('world_id'):
            return JSONResponse({'error':'Conversation unavailable'},404)
        from backend.app.world import realtime_world
        snapshot['data']=await asyncio.to_thread(realtime_world.sync,user,conversation,snapshot['data'])
        world = snapshot
        if channel != "group" and request.url.path != "/api/conversations":
            import hashlib
            child = hashlib.sha256((conversation + ":private:" + channel).encode()).hexdigest()[:48]
            try:
                snapshot = await asyncio.to_thread(storage.load_request_data, user, child)
            except PermissionError:
                await asyncio.to_thread(storage.get_store().ensure, user, child,
                                        {"world_id": conversation, "channel": channel, "title": PRIVATE_NAMES[channel], "scene_state": {}, "recent_log": []})
                snapshot = await asyncio.to_thread(storage.load_request_data, user, child)
            conversation = child
    except PermissionError:
        return JSONResponse({"error": "Conversation unavailable"}, 404)
    token = storage.scope.set((user, conversation))
    snapshot_token = storage.request_snapshot.set(snapshot)
    world_token = chat_world.set({"channel": channel, "id": world["scope"][1], "data": world["data"], "read_only": request.method == "GET"})
    try:
        return await call_next(request)
    finally:
        storage.request_snapshot.reset(snapshot_token)
        chat_world.reset(world_token)
        storage.scope.reset(token)

from backend.app.api.messages import list_conversations


from backend.app.api.auth import AccountIn


from backend.app.api.auth import account_status


from backend.app.api.auth import register_account


from backend.app.api.auth import login_account


from backend.app.api.auth import auth_rate_limit


from backend.app.api.auth import LogoutIn


from backend.app.api.auth import PasswordIn


from backend.app.api.auth import change_account_password


from backend.app.api.auth import logout_account


from backend.app.api.home import HomeSettingsIn


from backend.app.api.home import home_settings


from backend.app.api.home import background_health


from backend.app.api.home import retry_background_task


from backend.app.api.home import update_home_settings


from backend.app.api.home import ResetStoryIn


from backend.app.api.home import reset_from_home


from backend.app.api.messages import ReadIn


from backend.app.api.messages import read_messages


from backend.app.api.messages import list_role_actions


from backend.app.api.messages import cancel_role_action


from backend.app.api.messages import check_role_intentions


from backend.app.api.messages import external_role_tick


from backend.app.api.messages import push_config


from backend.app.api.messages import push_subscribe


from backend.app.api.messages import push_unsubscribe

from backend.app.api.messages import export_memory


from backend.app.api.messages import ConversationProfileIn


from backend.app.api.messages import conversation_profile


from backend.app.api.messages import update_conversation_profile


# ---------------------------------------------------------------- 配置 & 素材

from backend.app.characters.engine import _load_env_file


from backend.app.characters.engine import load_config


from backend.app.characters.engine import write_memory_pack



from functools import lru_cache


from backend.app.characters.engine import _cached_prompt


from backend.app.characters.engine import read_text


from backend.app.characters.engine import personal_memory


from backend.app.characters.engine import character_profile


from backend.app.characters.engine import build_prompt_parts


from backend.app.characters.engine import build_system_prompt


from backend.app.characters.engine import build_fewshot


# ---------------------------------------------------------------- 接口

from backend.app.core.schemas import ChatIn


from backend.app.api.messages import get_messages


from backend.app.api.messages import clear_message_display


from backend.app.api.messages import import_messages


@app.get("/api/info")
def info(details: bool = True):
    cfg = load_config()
    system = build_system_prompt() if details else ''
    return {
        "model": cfg.get("model") or "(未配置)",
        "base_url": cfg.get("base_url") or "(未配置)",
        "inject_chars": 0,  # Retained response field for older clients.
        "has_key": bool(cfg.get("api_key")),
        "system_chars": len(system),
        "fewshot_count": len(build_fewshot()) if details else 0,
        "scene_state": displayed_scene(),
        "role_avatars": role_avatars(),
    }


@app.get('/api/scene')
def current_scene_status():
    return {'scene_state':displayed_scene()}


from backend.app.characters.engine import shared_world_data


from backend.app.characters.engine import displayed_scene


from backend.app.characters.engine import model_scene


from backend.app.characters.engine import role_avatars


from backend.app.api.settings import SettingsIn


from backend.app.characters.engine import mask_key


from backend.app.api.settings import HomeModelIn


from backend.app.api.settings import home_model_draft


from backend.app.api.settings import home_model


from backend.app.api.settings import save_home_model


from backend.app.api.settings import test_home_model


from backend.app.api.settings import usage_report


from backend.app.characters.engine import story_root_id


from backend.app.characters.engine import read_story_flag


from backend.app.characters.engine import write_story_flag


from backend.app.api.settings import get_settings


from backend.app.api.settings import set_settings


from backend.app.api.settings import test_model


from backend.app.characters.engine import friendly_error


from backend.app.characters.engine import make_client


from backend.app.characters.engine import make_kwargs


from backend.app.characters.engine import check_cfg



from backend.app.characters.engine import sse


from backend.app.characters.engine import recent_phrases


from backend.app.characters.engine import actor_payload


from backend.app.api.messages import accept_message


from backend.app.api.messages import accepted_message_state


from backend.app.api.messages import retry_accepted_message


from backend.app.characters.engine import chat_stream
app.post("/api/stream")(chat_stream)


from backend.app.characters.engine import SCENE_FIELDS
from backend.app.characters.engine import SCENE_SYSTEM


from backend.app.characters.engine import update_scene_state



DESCRIBE_SAFE_SYSTEM = """你在替用户写他这一轮的动作和台词：给 3 条可直接发送的候选，风格自然、日常、不越界。

【三条的差异】不要三条同一个调子：
1. 延续当前话题，把对话往前推一步。
2. 换一个角度：关心她、提出安排，或转到更轻松的话题。
3. 比较直接的一条：把想说的话说清楚。

【跟着用户走】顺着用户最近几轮的用词、口吻和意图继续，不改他的语气，不替他收回已经说出的话。

【边界】这一档是普通聊天推荐：不写性、露骨、暴力或侮辱性的内容，不用身体部位暗示，不把对话推向亲密场景。
如果当前对话已经进入亲密场景，写成体贴的照顾、自然的过渡或把话题带开的一句，不描写过程。

【人称】固定为：「我」= 用户本人，「你」= 对面的角色。禁止用「他」「她」「小婉」当第三人称转述动作或感受。

【格式】每条一句话，动作写在括号里且括号内不写「我」，后接一句直接对角色说的话。不写旁白、分析或说明。
只输出JSON字符串数组，共3项。"""

DESCRIBE_SYSTEM = """你在替用户写他这一轮的动作和台词：给3条可直接发送的候选。用户要的是色情推进，不是聊天。

【三条的强度】按递进排，不要三条同一个调子：
1. 接住当前场景，把已经在做的事往前推一步：贴近、上手、说出自己的身体状态。
2. 加码：把前戏推向正式动作——脱、进、换位、加深、要得更明确。
3. 最直接的一条：一句话就要求，命令式，露骨到不需要铺垫。

【跟着用户走】顺着用户最近几轮已经表达的想法、癖好、称呼、体位和节奏继续，不改他的口吻，不替他收回要求。
不替角色拒绝、不劝退、不把话题拉回日常、不写「先聊一会儿」这种挡回去的候选。

【不在性场景时也要不正经】当前是日常对话时，3条仍要带色气：视线往哪儿放、身上哪里先反应、故意的贴近、一句半开的荤话、只穿一点、把他叫进房间。
不要写成干巴巴的日常对白，也不要把三条都写成纯情。

【人称】固定为：**「我」= 用户本人，「你」= 对面的角色。**三条候选每一条都只能这样写。
- 禁止用「他」「她」「小婉」当第三人称转述动作或感受：不写「我伸手摸她」，写「我伸手摸你」。
- 禁止写角色的动作、台词、体感和反应：「撑开的时候胀」「她轻声叫了一下」这种是她的部分，不是候选。
- 用户自己写第三人称时也照此改写，不要跟着用「她」。

【用词】直接写：肉棒、龟头、小穴、阴唇、阴蒂、淫水、插进来、射进来、灌满。不用「那里」「下面」「那个」代称。

【边界】只写用户本人的动作或台词，不替角色决定、回复、说话或移动。已发生的最新行动优先于旧状态，计划不能当成已完成。

【格式】每条一句话，动作写在括号里，后接一句直接对角色说的话；不写旁白、分析或说明。
只输出JSON字符串数组，共3项。"""

DESCRIBE_CAST = {'小婉': '青春·爱闹·玩心型主动', '小研': '甜妹·黏人·用撒娇索要', '小柳': '御姐·冷静·命令句'}


from backend.app.characters.engine import scene_brief


@app.post("/api/describe")
async def describe(body: ChatIn):
    """帮用户想下一步：返回 3 条可直接发送的动作描述。"""
    cfg = model_usage.config('suggestion',{})
    problem = check_cfg(cfg)
    if problem:
        return JSONResponse({"error": problem}, 400)

    transcript = []
    for m in body.messages[-8:]:
        content = (m.get('content') or '').strip()
        # Label the user's own lines as 我 so the model keeps one fixed point of view.
        transcript.append(content if m.get("role") != "user" else f"我：{content}")
    scene = "\n".join(transcript)

    brief = scene_brief()
    if brief:
        context = (
            f"【当前场景（可能已过时，以对话为准）】\n{brief}\n\n"
            f"【最近的对话（最新，优先）】\n{scene}"
        )
    else:
        context = f"【最近的对话】\n{scene}"

    channel = (chat_world.get() or {}).get('channel', 'group')
    names = [PRIVATE_NAMES[channel]] if channel in PRIVATE_NAMES else list(PRIVATE_NAMES.values())
    cast = '、'.join(f"{name}（{DESCRIBE_CAST.get(name, '')}）" for name in names)

    adult = bool(read_story_flag('adult_mode', False))
    system = DESCRIBE_SYSTEM if adult else DESCRIBE_SAFE_SYSTEM
    task = ("按强度递进给我 3 条下一步候选" if adult
            else "给我 3 条不同方向的下一步候选")
    payload = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{context}\n\n【在场角色】{cast}\n\n【任务】{task}，只输出 JSON 数组。"},
    ]

    client = make_client(cfg)
    raw = ""
    last_exc: Exception | None = None
    for _ in range(2):   # 连接抖一下就重试
        try:
            resp = await model_usage.complete(client,cfg,payload,'suggestion',
                temperature=1.15 if adult else 0.9,max_tokens=700)
            raw = (resp.choices[0].message.content or "").strip()
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        return JSONResponse({"error": friendly_error(last_exc)}, 502)

    options: list[str] = []
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            options = [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            options = []
    if not options:
        for line in raw.split("\n"):
            line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line).strip().strip('",')
            if line and not line.startswith("["):
                options.append(line)

    # A candidate must be the user's own line; drop any that writes a character's speech.
    as_role = re.compile(r'(?:小婉|小研|小柳)\s*[：:]')
    options = [option for option in options if not as_role.search(option)]

    return {"options": options[:3]}


from backend.app.characters.engine import chat
app.post("/api/chat")(chat)


from backend.app.api.messages import save_memory


# ---------------------------------------------------------------- 静态页面

STATIC = ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount('/web',StaticFiles(directory=STATIC/'web',check_dir=False),name='web')


def frontend_page():
    entry=STATIC/'web'/'index.html'
    if not entry.is_file():
        return JSONResponse({'error':'前端尚未构建，请在 frontend 目录运行 npm ci 和 npm run build'},503,headers={'Cache-Control':'no-store'})
    return FileResponse(entry,headers={'Cache-Control':'no-store'})


@app.get("/")
def home():
    return frontend_page()


@app.get('/sw.js')
def service_worker():
    return FileResponse(STATIC/'sw.js',media_type='application/javascript',headers={'Service-Worker-Allowed':'/','Cache-Control':'no-cache'})


@app.get("/chat")
def index():
    return frontend_page()


@app.get('/login')
def login_page():
    return frontend_page()

# Bind adapters after all application functions exist.
import sys
from backend.app.core import services as application_services
application_services.bind(sys.modules[__name__])

from backend.app.api.mcp_keys import router as mcp_keys_router
from backend.app.mcp.server import host as mcp_host
app.include_router(mcp_keys_router)
from backend.app.api.images import router as images_router
app.include_router(images_router)
app.mount('/mcp',mcp_host)

@app.on_event('startup')
async def start_mcp():await mcp_host.start()

@app.on_event('shutdown')
async def stop_mcp():await mcp_host.stop()

from backend.app.api.auth import router as auth_router
app.include_router(auth_router)
from backend.app.api.home import router as home_router
app.include_router(home_router)
from backend.app.api.settings import router as settings_router
app.include_router(settings_router)
from backend.app.api.messages import router as messages_router
app.include_router(messages_router)

from backend.app.infrastructure import events as event_bus
event_bus.install()
from backend.app.api.events import router as events_router
app.include_router(events_router)

@app.on_event('shutdown')
async def close_graph_resources():
    from backend.app.memory.retrieval import wait_pending
    await asyncio.to_thread(wait_pending)
    await asyncio.to_thread(model_usage.flush,force=True)
    from backend.app.runtime.graph import close_resources
    await asyncio.to_thread(close_resources)

def wake_maintenance():
    worker=getattr(app.state,'maintenance_worker',None)
    if worker:worker.wake.set()
