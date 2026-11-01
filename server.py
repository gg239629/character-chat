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
import model_usage
import model_defaults
import world_events
import peer_chat
import chat_inbox
import task_health
import accounts
import message_service

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "model.json"
PROMPT_FILE = ROOT / "prompt" / "system.md"
INJECT_FILE = ROOT / "prompt" / "inject.zh.md"
MEMORY_DIR = ROOT / "memory"
MEMORY_MD = MEMORY_DIR / "陪伴角色.md"
MEMORY_JSON = MEMORY_DIR / "记忆包.json"

from role_routing import recent_rounds
import long_memory
import storage
import actors
import chat_history
import secrets_store
import role_actions
import push_notifications
import maintenance
import scene_labels
import role_memory
import group_coordinator
import world_time
from fastapi import Request
from uuid import uuid4
from contextvars import ContextVar

chat_world = ContextVar("chat_world", default=None)
scene_revision = ContextVar("scene_revision", default=None)
PRIVATE_NAMES = {"wan": "小婉", "yan": "小研", "liu": "小柳"}

app = FastAPI(title="陪伴角色 · 网页版")

@app.on_event("startup")
def initialize_database():
    storage.initialize()
    import schema_migrations
    schema_migrations.upgrade(storage.get_store().engine)
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
        import memory_autosave
        await asyncio.to_thread(memory_autosave.flush_due,force=True)
    import model_usage
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
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path=='/api/jobs/tick':
        return await call_next(request)
    if request.url.path.startswith('/api/auth/'):
        return await call_next(request)
    if accounts.enabled():
        identity=await asyncio.to_thread(accounts.identify,request.cookies.get('chat_session',''))
        if not identity:return JSONResponse({'error':'请先登录'},401)
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
        world = snapshot
        if channel != "group" and request.url.path != "/api/conversations":
            import hashlib
            child = hashlib.sha256((conversation + ":private:" + channel).encode()).hexdigest()[:48]
            await asyncio.to_thread(storage.get_store().ensure, user, child,
                                    {"world_id": conversation, "channel": channel, "title": PRIVATE_NAMES[channel], "scene_state": {}, "recent_log": []})
            conversation = child
            snapshot = await asyncio.to_thread(storage.load_request_data, user, child)
    except PermissionError:
        return JSONResponse({"error": "Conversation unavailable"}, 404)
    token = storage.scope.set((user, conversation))
    snapshot_token = storage.request_snapshot.set(snapshot)
    world_token = chat_world.set({"channel": channel, "id": world["scope"][1], "data": world["data"]})
    try:
        return await call_next(request)
    finally:
        storage.request_snapshot.reset(snapshot_token)
        chat_world.reset(world_token)
        storage.scope.reset(token)

@app.get("/api/conversations")
def list_conversations():
    stories=storage.get_store().conversations(storage.scope.get()[0])
    summaries=chat_history.summaries()
    for story in stories:
        story['activity']={'group':summaries.get(story['id'],{})}
        for role in PRIVATE_NAMES:
            story['activity'][role]=summaries.get(actors.role_id(story['id'],role),{})
    return stories


class AccountIn(BaseModel):
    username: str = Field(min_length=1,max_length=40)
    password: str = Field(min_length=1,max_length=128)


@app.get('/api/auth/status')
def account_status(request: Request):
    return {'enabled':accounts.enabled(),'logged_in':bool(accounts.identify(request.cookies.get('chat_session',''))) if accounts.enabled() else False}


@app.post('/api/auth/register')
def register_account(body: AccountIn,request: Request):
    if not accounts.enabled():raise HTTPException(404,'账号功能尚未启用')
    auth_rate_limit(request)
    try:accounts.register(body.username,body.password)
    except ValueError as error:raise HTTPException(422,str(error))
    return {'ok':True}


@app.post('/api/auth/login')
def login_account(body: AccountIn,request: Request):
    if not accounts.enabled():raise HTTPException(404,'账号功能尚未启用')
    auth_rate_limit(request)
    try:token=accounts.login(body.username,body.password)
    except ValueError as error:raise HTTPException(401,str(error))
    response=JSONResponse({'ok':True})
    response.set_cookie('chat_session',token,httponly=True,secure=request.url.scheme=='https',samesite='strict',max_age=7*86400)
    return response


def auth_rate_limit(request):
    import hashlib
    identity=hashlib.sha256(('auth:'+str(request.client.host if request.client else 'unknown')).encode()).hexdigest()[:40]
    if not role_actions.claim_check('auth-'+identity,__import__('time').time(),1):
        raise HTTPException(429,'操作过于频繁，请稍后再试')


@app.post('/api/auth/logout')
def logout_account(request: Request):
    accounts.logout(request.cookies.get('chat_session',''))
    response=JSONResponse({'ok':True});response.delete_cookie('chat_session');return response


class HomeSettingsIn(BaseModel):
    initiative_enabled: bool


@app.get('/api/home/settings')
def home_settings():
    import initiative_control
    return {'initiative_enabled':initiative_control.enabled(storage.scope.get()[0],chat_world.get()['id'])}


@app.get('/api/home/task-health')
def background_health():
    return {'failures':task_health.report(storage.scope.get()[0],chat_world.get()['id'])}


@app.post('/api/home/tasks/{stage}/{identity}/retry')
def retry_background_task(stage:str,identity:str):
    try:return task_health.retry(storage.scope.get()[0],chat_world.get()['id'],stage,identity)
    except PermissionError:raise HTTPException(404,'Task unavailable')
    except ValueError as error:raise HTTPException(409,str(error))


@app.put('/api/home/settings')
def update_home_settings(body: HomeSettingsIn):
    import initiative_control
    value=initiative_control.set_enabled(storage.scope.get()[0],chat_world.get()['id'],body.initiative_enabled)
    return {'initiative_enabled':value}


class ResetStoryIn(BaseModel):
    confirm: bool = False


@app.post('/api/home/reset')
def reset_from_home(body: ResetStoryIn):
    if not body.confirm:raise HTTPException(422,'请确认删除聊天记录与记忆')
    import story_reset
    try:
        return story_reset.reset_story(storage.scope.get()[0],chat_world.get()['id'],backup=False)
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))


class ReadIn(BaseModel):
    message_id: str = Field(max_length=64)


@app.post('/api/messages/read')
def read_messages(body: ReadIn):
    try:
        chat_history.mark_read(body.message_id)
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))
    return {'ok':True}


@app.get('/api/actions')
def list_role_actions():
    world=chat_world.get()
    actor=world['channel'] if world['channel'] in PRIVATE_NAMES else None
    return {'actions':role_actions.audit(storage.scope.get()[0],world['id'],actor)}


@app.post('/api/actions/{identity}/cancel')
def cancel_role_action(identity: str):
    world=chat_world.get()
    if world['channel'] in PRIVATE_NAMES:
        available={item['id'] for item in role_actions.audit(storage.scope.get()[0],world['id'],world['channel'])}
        if identity not in available:
            raise HTTPException(404,'找不到当前角色的行动')
    if not role_actions.cancel(storage.scope.get()[0],world['id'],identity):
        raise HTTPException(409,'此行动已执行或无法取消')
    return {'ok':True}


@app.post('/api/actions/check')
async def check_role_intentions():
    world=chat_world.get()
    if world['channel'] not in PRIVATE_NAMES:
        raise HTTPException(400,'请在角色私聊中检查主动意图')
    result=await role_actions.autonomous(storage.scope.get()[0],world['id'],world['channel'])
    if result and result.get('target') in PRIVATE_NAMES:
        result=None  # Internal contact must not expose a hidden message through this API.
    return {'action':result,'message':'已检查；未满足条件或角色没有意愿时不会发消息。'}


@app.post('/api/jobs/tick')
async def external_role_tick(request: Request):
    import secrets
    token=os.getenv('ROLE_CRON_TOKEN','')
    supplied=request.headers.get('Authorization','')
    if len(token)<32 or not secrets.compare_digest(supplied,'Bearer '+token):
        raise HTTPException(401,'Scheduler authentication required')
    await role_actions.tick()
    return {'ok':True}


@app.get('/api/push/config')
def push_config():
    try:
        pair=push_notifications.keys()
        return {'public_key':pair[1] if pair else None}
    except Exception:
        return {'public_key':None}


@app.post('/api/push/subscribe')
def push_subscribe(body: dict):
    try:
        identity=push_notifications.subscribe(storage.scope.get()[0],body)
    except (ValueError,PermissionError) as exc:
        raise HTTPException(422,str(exc))
    return {'ok':True,'id':identity}


@app.post('/api/push/unsubscribe')
def push_unsubscribe(body: dict):
    endpoint=body.get('endpoint','')
    if not isinstance(endpoint,str) or len(endpoint)>3000:
        raise HTTPException(422,'Invalid endpoint')
    push_notifications.unsubscribe(storage.scope.get()[0],endpoint)
    return {'ok':True}

@app.get("/api/memory/export")
def export_memory():
    pack = storage.memory_pack()
    pack["scene_state"] = displayed_scene()
    return pack


class ConversationProfileIn(BaseModel):
    title: str
    avatar: str = ""


@app.get("/api/conversation/profile")
def conversation_profile():
    data = storage.current_data()
    identity = storage.scope.get()[1]
    return {"id": identity, "title": data.get("title") or ("原有故事" if identity == "default" else "未命名故事"), "avatar": data.get("avatar", "")}


@app.put("/api/conversation/profile")
def update_conversation_profile(body: ConversationProfileIn):
    import base64
    title = body.title.strip()
    if not title or len(title) > 60:
        raise HTTPException(422, "会话名称应为 1–60 个字")
    if body.avatar:
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
    return conversation_profile()


# ---------------------------------------------------------------- 配置 & 素材

def _load_env_file(path: Path) -> None:
    """把 .env 读进环境变量（不覆盖已有的）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    if storage.scope.get()[0] != "owner":
        cfg = {}

    env_file = cfg.get("env_file")
    if env_file:
        _load_env_file(Path(env_file))

    prefix = cfg.get("env_prefix", "DASHSCOPE")
    for key in ("base_url", "api_key", "model"):
        fallback = os.getenv(f"{prefix}_{key.upper()}", "") if storage.scope.get()[0] == "owner" else ""
        cfg[key] = cfg.get(key) or fallback
    cfg.setdefault("temperature", 1.0)
    cfg.setdefault("max_tokens", 1500)
    cfg.setdefault("timeout", 120)
    if storage.scope.get()[0] == "owner":
        for key in ("api_key", "base_url", "model"):
            cfg[key] = os.getenv("MODEL_" + key.upper()) or cfg.get(key, "")
    defaults=model_defaults.read(storage.scope.get()[0])
    cfg.update(defaults)
    if not defaults or storage.read_document('model_override',False):
        cfg.update(secrets_store.decode_settings(storage.read_document("settings", {})))
    return cfg


def write_memory_pack(pack: dict[str, Any]) -> None:
    """Update only the scene so concurrent history saves are preserved."""
    storage.write_document("scene_state", pack.get("scene_state") or {})



from functools import lru_cache


@lru_cache(maxsize=24)
def _cached_prompt(path,mtime,size):
    return Path(path).read_text(encoding='utf-8')


def read_text(path: Path) -> str:
    try:
        info=path.stat()
        return _cached_prompt(str(path),info.st_mtime_ns,info.st_size)
    except FileNotFoundError:return ''


def personal_memory(role: str) -> Path:
    """A role's own archive file, resolved next to MEMORY_MD so tests and hosts can relocate it."""
    return MEMORY_MD.parent / f"{role}.md"


def character_profile(role: str | None = None) -> str:
    """Shared rules plus this role's own file. The archive stays whole; a reset only clears chat data."""
    text = read_text(MEMORY_MD)
    if role:
        text += "\n" + read_text(personal_memory(role))
    return text


def build_prompt_parts(role: str | None = None) -> tuple[str, str]:
    """返回 (稳定块, 动态块)。稳定块每轮不变，放前面吃前缀缓存；动态块只有场景状态。"""
    stable: list[str] = []

    prompt_text = read_text(PROMPT_FILE).strip()
    if prompt_text:
        stable.append(prompt_text)

    # Optional run-level layer: how to write, not who to write. Missing file is fine.
    inject_text = read_text(INJECT_FILE).strip()
    if inject_text:
        stable.append(inject_text)

    memory_text = read_text(MEMORY_MD).strip()
    if role:
        # One actor only ever reads the shared archive plus her own file.
        memory_text = (memory_text + "\n\n" + read_text(personal_memory(role)).strip()).strip()
    if shared_world_data().get('memory_reset_at'):
        # The archive is reference material and survives a reset intact; only chat history is cleared.
        stable.append('这是已重置的新故事，从第一天清晨开始。数据库里的旧聊天、场景和关系进展不作为已经发生过的事；'
                      '角色档案里的性格、身体设定、关系背景和共同经历照旧有效，可以当作她记得的事来引用。')
    if memory_text:
        stable.append("# 角色档案与记忆\n" + memory_text)

    dynamic: list[str] = []
    state = model_scene()
    if state:
        dynamic.append("# 当前场景状态\n" + json.dumps(state, ensure_ascii=False, indent=2))

    s = "\n\n".join(stable) if stable else "按系统指定角色自然回应当前消息。"
    d = "\n\n".join(dynamic)
    return s, d


def build_system_prompt(role: str | None = None) -> str:
    """兼容旧调用：稳定块 + 动态块拼在一起。"""
    s, d = build_prompt_parts(role)
    return (s + "\n\n" + d).strip() if d else s


def build_fewshot(limit: int = 10) -> list[dict[str, str]]:
    """把记忆包里的最近对话转成 few-shot，语气照这个来。"""
    try:
        pack = storage.memory_pack()
    except json.JSONDecodeError:
        return []

    log = pack.get("recent_log") or []
    out: list[dict[str, str]] = []
    for item in log:
        who = str(item.get("who", "")).strip()
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if who in ("他", "用户", "user", "你"):
            item = {"role": "user", "content": text}
        else:
            item = {"role": "assistant", "content": f"{who}：{text}"}
        if out and out[-1] == item:      # 连续重复的不喂
            continue
        out.append(item)
    return recent_rounds(out, limit)


# ---------------------------------------------------------------- 接口

class ChatIn(BaseModel):
    messages: list[dict[str, str]]
    temperature: float | None = None
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')


@app.get("/api/messages")
def get_messages(after: str = '', incremental: bool = False):
    chat_history.bootstrap()
    import memory_autosave
    user,conversation=storage.scope.get()
    due=memory_autosave.checkpoint_due(user,conversation)
    if incremental:messages,unchanged,delta=chat_history.updates(after)
    else:
        messages,unchanged=chat_history.poll_visible(after);delta=False
    return {"messages": [] if unchanged else messages, "unchanged":unchanged,"delta":delta,"auto_saved": not bool(due),
            "save_pending": bool(due), "next_save_at": due,
            "display_cleared_at": storage.read_document("display_cleared_at",0)}


@app.post('/api/messages/clear-display')
def clear_message_display():
    chat_history.bootstrap()
    cutoff=chat_history.clear_display()
    return {'ok':True,'messages':[],'display_cleared_at':cutoff}


@app.post("/api/messages/import")
def import_messages(body: ChatIn):
    if len(body.messages) > 200 or any(len(m.get('content', '')) > 100000 or m.get('role') not in ('user','assistant') for m in body.messages):
        raise HTTPException(422, '浏览器历史格式无效或过长')
    chat_history.bootstrap()
    return {"imported": chat_history.import_browser(body.messages), "messages": chat_history.visible_messages()}


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


def shared_world_data():
    world = chat_world.get()
    identity = world['id'] if world else storage.scope.get()[1]
    return storage.get_store().read(storage.scope.get()[0], identity)


def displayed_scene():
    """All windows show the same live world and actor states."""
    data = shared_world_data()
    state = dict(data.get('scene_state', {}))
    for role, value in data.get('role_states', {}).items():
        if role in PRIVATE_NAMES:
            state[PRIVATE_NAMES[role]] = value.get('status', state.get(PRIVATE_NAMES[role], '未知'))
    for name in PRIVATE_NAMES.values():
        if name in state:state[name]=scene_labels.label(state[name])
    return state


def model_scene():
    """Shared public facts plus this actor's own state; private facts aren't omniscient."""
    data = shared_world_data()
    state = dict(data.get('scene_state', {}))
    world = chat_world.get()
    role = world['channel'] if world else 'group'
    own = data.get('role_states', {}).get(role, {})
    if role in PRIVATE_NAMES and own.get('status'):
        state[PRIVATE_NAMES[role]] = own['status']
    return state


def role_avatars():
    world = chat_world.get()
    identity = world["id"] if world else storage.scope.get()[1]
    items = storage.get_store().conversations(storage.scope.get()[0])
    story = next((item for item in items if item["id"] == identity), {})
    return {name: story.get("channels", {}).get(channel, {}).get("avatar", "") for channel, name in PRIVATE_NAMES.items()}


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


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "•" * 6 + key[-4:]


class HomeModelIn(BaseModel):
    model:str=Field(default='',max_length=200)
    base_url:str=Field(default='',max_length=2000)
    api_key:str=Field(default='',max_length=4000)
    temperature:float=Field(default=1,ge=0,le=2)
    max_tokens:int=Field(default=1500,ge=0,le=100000)


def home_model_draft(body):
    cfg=model_defaults.read(storage.scope.get()[0])
    cfg.update(model=body.model.strip(),base_url=body.base_url.strip(),temperature=body.temperature,
        max_tokens=body.max_tokens or None,timeout=120)
    if body.api_key.strip():cfg['api_key']=body.api_key.strip()
    return cfg


@app.get('/api/home/model')
def home_model():
    import hashlib
    user=storage.scope.get()[0];cfg=model_defaults.read(user)
    return {'model':cfg.get('model',''),'base_url':cfg.get('base_url',''),
        'temperature':cfg.get('temperature',1),'max_tokens':cfg.get('max_tokens',1500),
        'api_key_masked':mask_key(cfg.get('api_key','')),'configured':bool(cfg.get('model') and cfg.get('api_key')),
        'tutorial_scope':hashlib.sha256(user.encode()).hexdigest()[:16]}


@app.put('/api/home/model')
def save_home_model(body:HomeModelIn):
    cfg=home_model_draft(body)
    if check_cfg(cfg):raise HTTPException(422,'请填写模型名称和API Key')
    try:model_defaults.save(storage.scope.get()[0],cfg)
    except ValueError:raise HTTPException(400,'无法加密保存密钥，请检查服务加密配置')
    return home_model()


@app.post('/api/home/model/test')
async def test_home_model(body:HomeModelIn):
    cfg=home_model_draft(body)
    if check_cfg(cfg):raise HTTPException(422,'请填写模型名称和API Key')
    client=make_client(cfg)
    try:
        response=await model_usage.complete(client,cfg,[{'role':'user','content':'只回复：好'}],'connection_test',temperature=0,max_tokens=40)
        return {'ok':bool(response.choices),'model':cfg['model']}
    except Exception as error:raise HTTPException(502,'连接失败：'+type(error).__name__)
    finally:await client.close()


@app.get('/api/usage')
def usage_report(days:int=7):
    return model_usage.report(storage.scope.get()[0],max(1,min(30,days)))


def story_root_id() -> str:
    world = chat_world.get()
    return world['id'] if world else storage.scope.get()[1]


def read_story_flag(name: str, default=False):
    """Story-wide switches live on the root conversation so all four channels agree."""
    user = storage.scope.get()[0]
    try:
        data = storage.get_store().read(user, story_root_id())
    except Exception:
        return default
    return data.get(name, default)


def write_story_flag(name: str, value) -> None:
    user = storage.scope.get()[0]
    storage.get_store().mutate(user, story_root_id(), lambda data: data.update({name: value}))


@app.get("/api/settings")
def get_settings():
    """配置文件里的值 + 实际生效的值。api_key 只给掩码。"""
    file_cfg = load_config()
    eff = load_config()
    return {
        "model": file_cfg.get("model", ""),
        "base_url": file_cfg.get("base_url", ""),
        "temperature": file_cfg.get("temperature", 1.0),
        "max_tokens": file_cfg.get("max_tokens", 1500),
        "api_key_masked": mask_key(file_cfg.get("api_key", "") or eff.get("api_key", "")),
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
        "adult_mode": bool(read_story_flag('adult_mode', False)),
        "purpose": "scene" if not chat_world.get() or chat_world.get()['channel'] == 'group' else "character",
        "inherit_model":not storage.read_document('model_override',False) if model_defaults.read(storage.scope.get()[0]) else not bool(storage.read_document('settings',{})),
    }


@app.post("/api/settings")
def set_settings(body: SettingsIn):
    """写回 config/model.json。api_key 留空表示不改。"""
    if body.behavior is not None:
        raise HTTPException(422,'角色主动行为由性格与日常互动决定，不支持手动配置')
    if body.memory_preferences is not None:
        if set(body.memory_preferences)-{'group_public','private_public'} or any(not .15<=v<=.85 for v in body.memory_preferences.values()):
            raise HTTPException(422,'记忆预算比例需在0.15至0.85之间')
    cfg = dict(load_config())

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
        write_story_flag('adult_mode', bool(body.adult_mode))
    return get_settings()


@app.post("/api/test")
async def test_model(body: SettingsIn | None = None):
    """Test draft settings without saving them; blank keys reuse the saved key."""
    cfg = dict(load_config())
    if body is not None and body.inherit_model is True:cfg=model_defaults.read(storage.scope.get()[0])
    if body is not None and body.inherit_model is not True:
        for key in ("model", "base_url", "api_key"):
            value = getattr(body, key)
            if value is not None and (key != "api_key" or value.strip()):
                cfg[key] = value.strip()
    problem = check_cfg(cfg)
    if problem:
        return JSONResponse({"error": problem}, 400)

    client = make_client(cfg)
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

    return JSONResponse({"error": friendly_error(last_exc)}, 502)


def friendly_error(exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    if "data_inspection" in msg or "inappropriate" in msg:
        side = "输入" if "Input" in msg else "输出"
        msg += (
            f"\n\n供应商的{side}内容审核拦的，不是代码问题。"
            "降审核层只能压低倾向，压不住这种硬性过滤。"
            "要真放开只能换不审内容的模型：本地 Ollama 最省事，"
            "或者在 config/model.json 里换成别家接口。"
        )
    return msg


def make_client(cfg: dict) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"] or None,
        timeout=cfg.get("timeout", 120),
    )


def make_kwargs(cfg: dict, body: "ChatIn") -> dict:
    kw: dict[str, Any] = dict(
        model=cfg["model"],
        temperature=body.temperature if body.temperature is not None else cfg["temperature"],
    )
    limit = cfg.get("max_tokens")
    if isinstance(limit, int) and limit > 0:   # 0 / null 表示不限制
        kw["max_tokens"] = limit
    return kw


def check_cfg(cfg: dict) -> str:
    if not cfg.get("api_key"):
        return "没有配置 api_key：填 config/model.json，或设置环境变量"
    if not cfg.get("model"):
        return "没有配置 model：填 config/model.json"
    return ""



def sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def recent_phrases(history, name, limit=6):
    """Short clauses this speaker already used; repeating them reads as a catchphrase."""
    used = []
    for message in reversed(history):
        if message.get('role') != 'assistant':
            continue
        text = message.get('content') or ''
        if not text.startswith((name + '：', name + ':')):
            continue
        body = re.sub(r'^小[婉研柳][：:]', '', text)
        for clause in re.split(r'[。！？\n]', re.sub(r'[（(][^（）()]*[）)]', '', body)):
            clause = clause.strip(' ，,、…—～~')
            if 4 <= len(clause) <= 20 and clause not in used:
                used.append(clause)
        if len(used) >= limit:
            break
    return used[:limit]


def actor_payload(role, messages, group):
    """Build one actor's context from their own memory and public information."""
    world = chat_world.get() or {"id": storage.scope.get()[1], "channel": "group", "data": storage.current_data()}
    user = storage.scope.get()[0]
    world = {**world, 'data': shared_world_data()}
    with actors.role_scope(user, world["id"], role):
        cfg = load_config()
        persona = storage.read_document("persona", "")
        relationship = storage.read_document("relationship", "")
        own_state=world['data'].get('role_states',{}).get(role,{})
        name = PRIVATE_NAMES[role]
        stable, _ = build_prompt_parts(role)
        # The status bar is omniscient; a character's prompt must not be.
        scene=world['data'].get('scene_state',{})
        visible_scene={'时间':world_time.display(world_time.clock(world['data'])),
            name:own_state.get('status') or scene.get(name,'未知')}
        current_context=recent_rounds(messages,10)
        receipt_fact=role_actions.received_contact_status(user,world['id'],role,
            next((m.get('content','') for m in reversed(messages) if m['role']=='user'),''))
        query=role_memory.query_for(messages)
        memory=role_memory.remember(user,world['id'],role,query,group,
            exclude_ids=[m['id'] for m in current_context if m.get('id')],
            exclude_texts=[m.get('content','') for m in current_context])
        payload=[{'role':'system','content':stable},
            {'role':'system','content':'共同世界场景（仅公开事实及你自己的状态）：'+json.dumps(visible_scene,ensure_ascii=False)},
            {'role':'system','content':role_memory.render(memory)}]
        import continuity
        payload.append({'role':'system','content':'明确设定与空间依据：'+json.dumps(continuity.context(user,world['id'],role),ensure_ascii=False,separators=(',',':'))})
        if receipt_fact:payload.append({'role':'system','content':'已核实的通信事实，用本人自然口吻回答，不照抄系统措辞：'+receipt_fact})
        payload += [{'role':m['role'],'content':m['content']} for m in current_context]
        progress=world['data'].get('personal_progress',{}).get(role,[])[-8:]
        payload.append({'role':'system','content':
            '全世界唯一的当前故事时间：'+world_time.display(world_time.clock(world['data']))+
            '。其他窗口旧聊天里的早晚只代表当时，不得把当前时间倒退。你有自己的生活进度，不知道其他人的私聊。'
            '下列为你本人的日常进度，只写你自己的活动；按日常推进的事件不能说成与用户约好、见面或别人转述。\n'+json.dumps(progress,ensure_ascii=False)})
        if own_state:
            payload.append({'role':'system','content':'你的最新个人状态（以共同故事时间为当前，其他人不自动知情）：'+json.dumps(own_state,ensure_ascii=False)})
        facts=world_events.knowledge(user,world['id'],role)
        import event_memory
        observations=event_memory.recall(user,world['id'],role,query)
        if observations:payload.append({'role':'system','content':'你本人实际行动的历史证据（时间是发生时刻，历史动作不等于现在仍在进行）：'+json.dumps(observations,ensure_ascii=False,separators=(',',':'))})
        receipts=world_events.receipts(user,world['id'],role)
        sent_actions=[{'target':a['target'],'status':a['status'],'message_id':a['detail'].get('message_id')}
            for a in role_actions.audit(user,world['id'],role,internal=True) if a['status']=='sent'][:6]
        if sent_actions:
            payload.append({'role':'system','content':'你本人真实执行过的通信回执（user=你与用户的私聊，group=群聊，其余为对应角色私聊；不代表新请求也已执行）：'+json.dumps(sent_actions,ensure_ascii=False)})
        if facts or receipts:
            payload.append({'role':'system','content':'你有依据知道的实际生活事件：'+json.dumps(facts,ensure_ascii=False,separators=(',',':'))+
                '\n实际群消息投递凭据：'+json.dumps(receipts,ensure_ascii=False,separators=(',',':'))+
                '\n这是世界事实，不是私聊原文。已做好的同一顿饭不能在另一个窗口变回水刚开或没熟；除非明确开始做新一锅。没有收到通知和饭没做好是两回事。'
                '不能把描写摸手机、点击发送当成真的发消息；没有系统投递凭据不得声称发到群里/其他私聊，不编造转圈、网络坏或信号差作为未发送原因。用户不在家时不要把她当成在厨房眼前的人。'})
        import actor_instructions
        payload.append({'role':'system','content':actor_instructions.render(name,group,persona,relationship)})
        used=recent_phrases(current_context,name)
        if used:
            payload.append({'role':'system','content':
                '你最近几轮已经说过的话：「'+'」「'.join(used)+'」。'
                '本轮一个字都不许重复这些句子，也不许换个语气把同一句再说一遍；'
                '换新的动作、新的身体感受、新的内容，不要用口头禅填满回复。'})
        return cfg, payload


@app.post('/api/send')
async def accept_message(body: ChatIn):
    try:
        return await asyncio.to_thread(chat_inbox.accept,body)
    except ValueError as exc:
        return JSONResponse({'error':str(exc)},422)


@app.get('/api/send/{identity}')
def accepted_message_state(identity: str):
    try:return chat_inbox.state(identity)
    except PermissionError:raise HTTPException(404,'Message unavailable')


@app.post('/api/send/{identity}/retry')
def retry_accepted_message(identity: str):
    try:return chat_inbox.retry(identity)
    except PermissionError:raise HTTPException(404,'Message unavailable')
    except ValueError as error:raise HTTPException(409,str(error))


@app.post("/api/stream")
async def chat_stream(body: ChatIn):
    """Sequential role generation; database messages are the canonical history."""
    current = next((m.get("content", "").strip() for m in reversed(body.messages) if m.get("role") == "user"), "")
    if not current or len(current) > 100000:
        return JSONResponse({"error":"请输入有效消息（不超过十万字）"},422)
    await asyncio.to_thread(chat_history.bootstrap)
    existing = await asyncio.to_thread(chat_history.messages)
    world = chat_world.get() or {"id":storage.scope.get()[1],"channel":"group","data":storage.current_data()}
    await asyncio.to_thread(world_events.ensure,storage.scope.get()[0],world['id'])
    group = world["channel"] == "group"
    history = [{"id":m["id"],"role":m["role"],"content":m["content"]} for m in existing]
    if not chat_inbox.processing.get():
        history.append({"id":chat_history.identity(storage.scope.get()[1],body.request_id,0),"role":"user","content":current})
    targets = actors.candidates(history) if group else [world["channel"]]
    if not targets:
        return JSONResponse({"error":"当前场景没有可回应的角色，请先明确交谈对象。"},400)
    # Evaluate explicit narrative time once; commit only after obtaining this turn's lease.
    old_clock=world_time.clock(shared_world_data())
    next_clock=world_time.advance_candidate(history,old_clock)
    # Validate configuration without building three unused memory prompts.
    prepared = {}
    def prepare_config(role):
        with actors.role_scope(storage.scope.get()[0],world['id'],role):return load_config()
    for role in targets:
        cfg = await asyncio.to_thread(prepare_config,role)
        prepared[role] = (cfg, [])
        problem = check_cfg(cfg)
        if problem:
            return JSONResponse({"error":PRIVATE_NAMES[role] + "：" + problem},400)
    try:
        claimed = await asyncio.to_thread(chat_history.claim,body.request_id,chat_inbox.processing.get())
    except RuntimeError as exc:
        return JSONResponse({"error":str(exc)},409)
    if not claimed:
        async def replay():
            yield sse({"done":True,"messages":await asyncio.to_thread(chat_history.visible_messages),"replayed":True})
        return StreamingResponse(replay(),media_type="text/event-stream")
    try:
        await asyncio.to_thread(chat_history.append,body.request_id,0,"user",current)
        records=await asyncio.to_thread(chat_history.messages)
        user_record=next(m for m in records if m['id']==chat_history.identity(storage.scope.get()[1],body.request_id,0))
        await asyncio.to_thread(world_events.capture,storage.scope.get()[0],world['id'],world['channel'],user_record)
        if next_clock!=old_clock:
            latest=await asyncio.to_thread(chat_history.messages,1)
            active={world['channel']} if not group else set()
            await asyncio.to_thread(storage.get_store().mutate,storage.scope.get()[0],world['id'],
                lambda data:world_time.apply(data,next_clock,active,latest[-1]['created_at']))
    except Exception:
        await asyncio.to_thread(chat_history.release,body.request_id)
        raise

    async def gen():
        completed = []
        try:
            requested_roles=role_actions.group_request_roles(current) if group else []
            async def private_speakers():
                for role in targets:yield role
            stream_roles=group_coordinator.speakers(history,prepared,requested_roles) if group else private_speakers()
            index=0;handled_requests=set()
            async for role in stream_roles:
                latest_history=await asyncio.to_thread(chat_history.messages)
                if any(m['role']=='user' and m['id']!=chat_history.identity(storage.scope.get()[1],body.request_id,0)
                       and m['created_at']>user_record['created_at'] for m in latest_history):
                    break  # Yield the floor to the newly accepted user turn.
                index+=1
                name = PRIVATE_NAMES[role]
                yield sse({"status": "正在等待群聊回应…" if group else name + "正在回复…", "speaker":name})
                cfg, payload = await asyncio.to_thread(actor_payload,role,history,group)
                cross=role_actions.resolve_target(history,role,world['channel']) if (not group or role in requested_roles) and role not in handled_requests else None
                action=None
                if cross:
                    handled_requests.add(role)
                    action=await role_actions.requested(role,cross,history,
                        chat_history.identity(storage.scope.get()[1],body.request_id,0))
                if action and action['status']!='none':
                    if action['status']=='pending' and action['due_at']<=__import__('time').time():
                        action=await role_actions.deliver(action['id'])
                    state=action['status']
                    if state=='sent':
                        if cross in PRIVATE_NAMES:
                            await asyncio.to_thread(peer_chat.queue,storage.scope.get()[0],world['id'],role,
                                'followup',action['id'],{'channel':world['channel'],'source':action['id'],
                                'question':current[-900:],'message_id':action['detail']['message_id'],
                                'created_at':__import__('time').time()},5)
                        import action_voice
                        if action['detail'].get('delivery_report'):
                            try:
                                voiced=await action_voice.phrase(role,cfg,action['detail']['delivery_report'],history,group)
                                response=voiced
                            except Exception:
                                response='我转告过她了。'
                        else:
                            response=action['detail'].get('sent_reply') or action['detail'].get('reply','')
                            if not action_voice.natural(response):response='嗯，跟'+PRIVATE_NAMES[cross]+'说了。' if cross in PRIVATE_NAMES else '嗯，发了。'
                    elif state=='pending':
                        response=action['detail'].get('reply') or '等我忙完这会儿再说。'
                    elif state=='failed':
                        response='刚才没发出去，我再看看。'
                    else:
                        response=action['detail'].get('reply') or ('这次我不想替你发。' if state=='refuse' else '你想让我具体发什么？')
                        if world_events.fake_delivery(response):response='我这次还没有实际发送。'+('这条我不想替你发。' if state=='refuse' else '我需要确认要发的内容。')
                    text=actors.normalize_reply(response,name)
                    yield sse({'delta':('\n\n' if completed else '')+text})
                    yield sse({'action':{'id':action['id'],'status':state,'target':cross}})
                else:
                    client = make_client(cfg)
                    raw = ""
                    interruption_check=0
                    prefix = "\n\n" if completed else ""
                    try:
                        with model_usage.Measure('character',cfg,payload) as metric:
                            options=make_kwargs(cfg,body)
                            if os.getenv('MODEL_STREAM_USAGE','true').lower() in ('true','1','yes'):
                                options['stream_options']={'include_usage':True}
                            try:
                                stream=await client.chat.completions.create(messages=payload,stream=True,**options)
                            except Exception as error:
                                # Some compatible providers reject only this optional usage feature.
                                if getattr(error,'status_code',None)==400 and any(key in str(error) for key in ('stream_options','include_usage')):
                                    options.pop('stream_options',None)
                                    stream=await client.chat.completions.create(messages=payload,stream=True,**options)
                                else:raise
                            async for chunk in stream:
                                if chat_inbox.processing.get() and __import__('time').monotonic()-interruption_check>.75:
                                    interruption_check=__import__('time').monotonic()
                                    newest=await asyncio.to_thread(chat_history.messages,1)
                                    if message_service.cancels_draft(newest,user_record['created_at']):
                                        break
                                metric.observe(chunk)
                                if not chunk.choices:continue
                                piece=chunk.choices[0].delta.content or ''
                                if piece:
                                    raw+=piece
                                    if not group:
                                        yield sse({'delta':prefix+piece,'speaker':name})
                                        prefix=''
                            metric.output(raw)
                    finally:
                        close=getattr(client,'close',None)
                        if close is not None:await close()
                    if group and raw.strip() in (actors.SILENCE, name+'：'+actors.SILENCE, name+':'+actors.SILENCE):
                        continue
                    if chat_inbox.processing.get():
                        newest=await asyncio.to_thread(chat_history.messages,1)
                        if message_service.cancels_draft(newest,user_record['created_at']):
                            break
                    text = actors.normalize_reply(raw,name)
                    facts=await asyncio.to_thread(world_events.knowledge,storage.scope.get()[0],world['id'],role)
                    if world_events.fake_delivery(text):
                        verified=await asyncio.to_thread(world_events.receipts,storage.scope.get()[0],world['id'],role)
                        own_receipts=[receipt for receipt in verified if receipt['actor']==name]
                        own_user_receipts=[a for a in await asyncio.to_thread(role_actions.audit,storage.scope.get()[0],world['id'],role,True)
                            if a['target']=='user' and a['status']=='sent']
                        if own_user_receipts and '群' not in current and re.search(r'是不是|发了吗|收到|发消息|发个|刚刚|刚才',current):
                            text=name+'：我确实已经在这个私聊给你发过消息，可以往前查看。'
                        elif own_receipts and re.search(r'发了吗|刚才.{0,8}发|你刚.{0,6}发',current):
                            text=name+'：我之前确实发过群消息，你可以到群里查看。'
                        else:text=name+'：这条还没有实际发到其他聊天里，不能算已经发送。'
                    if world_events.unsupported_handover(text):
                        text=name+'：我还没有通过实际通信把这件事转达给对方，不能算已经说到了。'
                    if world_events.meal_conflict(text,facts):
                        meal=facts['meal'];text=name+'：'+meal['cook']+'已经把'+meal['food']+'做好了。'
                    if group:
                        import reply_quality
                        text=reply_quality.trim_repeated(text,history)
                        if not text:continue
                        if reply_quality.redundant(text,completed):
                            break
                        yield sse({'delta':prefix+text,'speaker':name})
                if not action:
                    latest_history=await asyncio.to_thread(chat_history.messages)
                    if message_service.cancels_draft(latest_history,user_record['created_at']):
                        break  # Discard unsent stale model text; next turn uses the interruption.
                metadata={'source':'role_action','action_id':action['id']} if action else None
                await asyncio.to_thread(chat_history.append,body.request_id,index,"assistant",text,name,True,metadata)
                completed.append(text)
                history.append({"id":chat_history.identity(storage.scope.get()[1],body.request_id,index),"role":"assistant","content":text})
                yield sse({"replace":"\n\n".join(completed)})
                saved = await asyncio.to_thread(chat_history.messages,1)
                if saved:
                    await asyncio.to_thread(world_events.capture,storage.scope.get()[0],world['id'],world['channel'],saved[-1])
                    yield sse({"saved_message":saved[-1]})
                    try:
                        if not action:
                            await asyncio.to_thread(peer_chat.enqueue_inquiry,storage.scope.get()[0],world['id'],role,
                                world['channel'],saved[-1]['id'],current,text)
                    except Exception:
                        pass  # Background inquiry must never hold up or undo a saved reply.
                    try:
                        await asyncio.to_thread(push_notifications.enqueue,storage.scope.get()[0],world['id'],world['channel'],saved[-1]['id'])
                    except Exception:
                        pass
            # Only durable writes remain on the response path; no maintenance model calls.
            queued=False
            try:
                latest=await asyncio.to_thread(chat_history.messages,1)
                if latest:
                    await asyncio.to_thread(maintenance.enqueue,storage.scope.get()[0],world['id'],world['channel'],
                        body.request_id,latest[-1]['created_at'],bool(completed))
                    queued=True
                    worker=getattr(app.state,'maintenance_worker',None)
                    if worker:worker.wake.set()
            except Exception:
                yield sse({'warning':'回复已保存，后台维护排队失败；聊天记录不受影响。'})
            yield sse({"done":True,"no_reply":group and not completed,"maintenance_pending":queued,
                "messages":await asyncio.to_thread(chat_history.visible_messages)})

        except Exception as exc:
            yield sse({"replace":"\n\n".join(completed)})
            yield sse({"error":friendly_error(exc),"messages":await asyncio.to_thread(chat_history.visible_messages)})
        finally:
            await asyncio.to_thread(chat_history.release,body.request_id)

    return StreamingResponse(gen(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


SCENE_FIELDS = ("时间", "地点", "在场人物", "小婉", "小研", "小柳", "主要交谈角色", "上一段停在")
SCENE_SYSTEM = """你是场记，只更新当前场景，不续写故事。
输出 JSON 对象，字段为：时间、地点、在场人物、小婉、小研、小柳、主要交谈角色、上一段停在。三个人物字段用对象 {"地点":"卧室","动作":"刚起床"}，地点和动作连同分隔符总计不超过10个字，只写物理地点和正在做什么，不写心理、对话、关系或历史；其他字段为字符串。
依据：旧场景、最近四轮完整对话，以及单独标出的本轮对话。本轮是更新重点。
- 你维护的是世界真实状态，不是角色对外声称的状态。旧场景中的真实位置和动作是基准；群聊与私聊都是手机消息，发言不代表物理在场。
- 严格区分实际行动叙述与台词：用户明确描述已发生的行动、角色括号动作叙述可作为实际变化；角色说“我在家”“我在睡觉”等自述、转述或聊天借口本身不构成实际位置/动作变化的证据，没有独立行动依据就保留旧状态。
- 欺骗、隐瞒、装作、谎称、开玩笑以及与真实状态冲突的台词，绝不覆盖真实状态。例如小婉真实在卧室看书，却群聊说在外面跑步，状态仍为卧室·看书。即使其他角色相信，也不改变事实。
- 不根据发言内容、在线/离线、是否回复来猜测地点和活动；不得将角色谎话写入地点、在场人物或其他世界事实字段。此世界事实仅供状态维护，不意味着其他角色自动知道。
- 用户明确的纠正、时间跳转和场景切换优先于旧场景及与之冲突的回复。例如快进到第二天，必须清理昨夜正在进行的动作。
- 只记录已经发生的事实。计划、愿望、提问、回忆、假设、引用不等于正在发生；准备去厨房不等于已经在厨房。
- 本轮明确变化覆盖旧状态；没有变化的保留。位置和动作改变时移除不再成立的旧状态，不要把新旧状态堆在一起。
- 没有依据的内容写“未知”，不补造位置、行为、心理或离场、在场事实。
- 在场人物只列有依据仍在当前场景的人；离场人物可在对应人物字段记录最后明确去向，不继续其旧动作。
- 主要交谈角色依据本轮最终回复首个角色标签；没有标签时参考最近对话，不默认小婉。
- 上一段停在只记录当前最后的事实或对话落点，不写下一步计划。
- 每个字段简短写事实，原则上不超过 50 字。不写剧情总结，不输出额外字段、解释或代码围栏。"""


def update_scene_state(messages: list[dict[str, str]], force: bool = False, cfg_override: dict | None = None) -> dict[str, str] | None:
    """每轮更新当前场景，不在记忆包中保存调度计数。"""
    try:
        pack = storage.memory_pack()
    except json.JSONDecodeError:
        return None

    data_before=shared_world_data()
    old_clock=world_time.clock(data_before)
    candidate_clock=world_time.advance_candidate(messages,old_clock)
    world=chat_world.get()
    active={world['channel']} if world and world['channel'] in PRIVATE_NAMES else set()
    # Streaming route already advanced time. Direct callers and recovery jobs use the same rule.
    # A turn revision prevents an older task from applying a relative "next day" twice.
    revision=scene_revision.get()
    if candidate_clock!=old_clock and revision is None:
        identity=world['id'] if world else storage.scope.get()[1]
        storage.get_store().mutate(storage.scope.get()[0],identity,
            lambda data:world_time.apply(data,candidate_clock,active,revision))
    old_state = displayed_scene()
    if not isinstance(old_state, dict):
        old_state = {}

    current_round = recent_rounds(messages, 1)
    transcript = []
    for m in recent_rounds(messages[:-len(current_round)] if current_round else messages, 3):
        who = "用户输入（区分行动叙述与提问/引述）" if m.get("role") == "user" else "手机聊天内容（台词不是实际行动证据）"
        transcript.append(f"{who}：{(m.get('content') or '').strip()}")

    current_round = recent_rounds(messages, 1)
    current_text = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '角色回复'}：{m.get('content') or ''}"
        for m in current_round
    )
    relevant=[PRIVATE_NAMES[world['channel']]] if world and world['channel'] in PRIVATE_NAMES else list(PRIVATE_NAMES.values())
    explicit={name:scene_labels.physical_transition(current_round,name,bool(world and world['channel'] in PRIVATE_NAMES)) for name in relevant}
    if any(explicit.values()):
        identity=world['id'] if world else storage.scope.get()[1]
        stamp=revision if revision is not None else __import__('time').time()
        def apply_explicit(data):
            for role,name in PRIVATE_NAMES.items():
                status=explicit.get(name)
                if not status:continue
                previous=data.get('role_states',{}).get(role,{})
                if stamp<previous.get('evidence_at',0):continue
                data.setdefault('role_states',{})[role]={**previous,'status':status,'source':'explicit_action','evidence_at':stamp}
                data.setdefault('scene_state',{})[name]=status
        storage.get_store().mutate(storage.scope.get()[0],identity,apply_explicit)
        # Clear completed movement even if the maintenance model is unavailable.
        if not any(scene_labels.has_action_evidence(current_round,name,bool(world and world['channel'] in PRIVATE_NAMES))
                   and not explicit.get(name) for name in relevant):return displayed_scene()
        old_state=displayed_scene()
    if not force and not any(scene_labels.has_action_evidence(current_round,name,bool(world and world['channel'] in PRIVATE_NAMES)) for name in relevant):
        return displayed_scene()
    cfg = model_usage.config('scene',cfg_override or {})
    if check_cfg(cfg):
        return None

    world=chat_world.get()
    private_rule=''
    if world and world['channel'] in PRIVATE_NAMES:
        private_rule='\n这是手机私聊。只更新'+PRIVATE_NAMES[world['channel']]+'本人明确已发生的动作、位置和状态；不能因为发消息就判定见面。计划、邀请和假设不算已执行。不把私聊秘密、约定内容或对话摘要写进人物状态，其他人物状态保留。'
    payload = [
        {"role": "system", "content": SCENE_SYSTEM+private_rule},
        {"role": "user", "content":
            "【当前场景状态】\n" + json.dumps(old_state, ensure_ascii=False, indent=2)
            + "\n\n【最近的对话】\n" + "\n".join(transcript)
            + "\n\n【本轮对话：更新重点】\n" + current_text
            + "\n\n【任务】输出更新后的场景状态 JSON。"},
    ]

    try:
        from openai import OpenAI
        cfg=model_usage.config('scene',cfg)
        with model_usage.Measure('scene',cfg,payload) as metric:
            with OpenAI(api_key=cfg['api_key'],base_url=cfg.get('base_url') or None,timeout=60) as client:
                resp=client.chat.completions.create(model=cfg['model'],messages=payload,temperature=.2,max_tokens=500)
                metric.observe(resp)
                raw=(resp.choices[0].message.content or '').strip();metric.output(raw)
    except Exception:
        return None

    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        new_state = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(new_state, dict) or not new_state:
        return None

    merged = {k: old_state.get(k, "未知") for k in SCENE_FIELDS}
    for k in SCENE_FIELDS:
        v = new_state.get(k)
        if k in PRIVATE_NAMES.values():
            merged[k]=(scene_labels.physical_transition(current_round,k,bool(world and world['channel'] in PRIVATE_NAMES)) or scene_labels.ground_location(current_round,k,v,old_state.get(k,'位置未知·待确认'))) if scene_labels.has_action_evidence(current_round,k,bool(world and world['channel'] in PRIVATE_NAMES)) else old_state.get(k,'位置未知·待确认')
        elif k=='时间':
            merged[k]=old_state.get('时间',world_time.display(world_time.clock(shared_world_data())))
        elif isinstance(v, str) and v.strip():
            merged[k] = v.strip()
    merged["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    world = chat_world.get()
    identity = world['id'] if world else storage.scope.get()[1]
    if world and world['channel'] in PRIVATE_NAMES:
        role = world['channel']; name = PRIVATE_NAMES[role]
        status = merged.get(name, '未知')
        if status and status != '未知':
            def change(data):
                revision=scene_revision.get()
                if revision is not None and (revision<=data.get('scene_applied_at',0) or revision<data.get('world_clock_revision',0)): return
                previous=data.get('role_states',{}).get(role,{})
                if revision is not None and revision<previous.get('evidence_at',0):return
                if revision is not None:data['scene_applied_at']=revision
                data.setdefault('role_states', {})[role] = {
                    **previous,'status': status, 'source': 'private', 'updated_at': merged['更新时间']}
            storage.get_store().mutate(storage.scope.get()[0], identity, change)
        # A private window owns history, not another physical world.
        storage.get_store().mutate(*storage.scope.get(), lambda data: data.pop('scene_state', None))
    else:
        changes = {key: value for key, value in merged.items()
                   if key != '更新时间' and value != old_state.get(key, '未知')}
        def change(data):
            revision=scene_revision.get()
            if revision is not None and (revision<=data.get('scene_applied_at',0) or revision<data.get('world_clock_revision',0)):return
            if revision is not None:data['scene_applied_at']=revision
            scene = data.setdefault('scene_state', {})
            # Patch only facts changed by this round; concurrent updates to other roles survive.
            for key, value in changes.items():
                if scene.get(key, '未知') == old_state.get(key, '未知') or key in PRIVATE_NAMES.values():
                    matching = next((role for role, name in PRIVATE_NAMES.items() if name == key), None)
                    if matching and revision is not None and revision<data.get('role_states',{}).get(matching,{}).get('evidence_at',0):continue
                    latest = data.get('role_states', {}).get(matching, {}).get('status', scene.get(key, '未知'))
                    if matching and latest != old_state.get(key, '未知'):
                        continue
                    scene[key] = value
                    if matching:
                        data.get('role_states', {}).pop(matching, None)
            scene['更新时间'] = merged['更新时间']
        storage.get_store().mutate(storage.scope.get()[0], identity, change)
    return displayed_scene()



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


def scene_brief() -> str:
    """把记忆包里的场景状态压成几行，给「描述」当上下文。"""
    try:
        pack = storage.memory_pack()
    except json.JSONDecodeError:
        return ""
    state = model_scene()
    if not state:
        return ""
    lines = []
    for k, v in state.items():
        if isinstance(v, (str, int, float)):
            lines.append(f"{k}：{v}")
    return "\n".join(lines)


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


@app.post("/api/chat")
async def chat(body: ChatIn):
    response = await chat_stream(body)
    if not isinstance(response, StreamingResponse):
        return response
    reply = ""
    async for block in response.body_iterator:
        if isinstance(block, bytes):
            block = block.decode()
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if "replace" in event:
                reply = event["replace"]
            elif event.get("delta"):
                reply += event["delta"]
            if event.get("error"):
                return JSONResponse({"error":event["error"]},502)
    return {"reply":reply}


@app.post("/api/save")
def save_memory(body: ChatIn):
    # Compatibility with older browsers: idempotently import, never append twice.
    chat_history.bootstrap()
    saved = chat_history.import_browser(body.messages)
    return {"saved":saved,"total":len(storage.memory_pack()["recent_log"]),
            "last_saved":datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------------------------------------------------------- 静态页面

STATIC = ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "home.html")


@app.get('/sw.js')
def service_worker():
    return FileResponse(STATIC/'sw.js',media_type='application/javascript',headers={'Service-Worker-Allowed':'/','Cache-Control':'no-cache'})


@app.get("/chat")
def index():
    return FileResponse(STATIC / "index.html")
