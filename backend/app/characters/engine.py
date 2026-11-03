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

from functools import lru_cache
from backend.app.core import services
from backend.app.core.schemas import ChatIn

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
    if services.models().CONFIG_FILE.exists():
        cfg = json.loads(services.models().CONFIG_FILE.read_text(encoding="utf-8"))

    if storage.scope.get()[0] != "owner":
        cfg = {}

    env_file = cfg.get("env_file")
    if env_file:
        services.models()._load_env_file(Path(env_file))

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

@lru_cache(maxsize=24)
def _cached_prompt(path,mtime,size):
    return Path(path).read_text(encoding='utf-8')

def read_text(path: Path) -> str:
    try:
        info=path.stat()
        return services.prompts()._cached_prompt(str(path),info.st_mtime_ns,info.st_size)
    except FileNotFoundError:return ''

def personal_memory(role: str) -> Path:
    """A role's own archive file, resolved next to MEMORY_MD so tests and hosts can relocate it."""
    return services.prompts().MEMORY_MD.parent / f"{role}.md"

def character_profile(role: str | None = None) -> str:
    """Shared rules plus this role's own file. The archive stays whole; a reset only clears chat data."""
    text = services.prompts().read_text(services.prompts().MEMORY_MD)
    if role:
        text += "\n" + services.prompts().read_text(services.prompts().personal_memory(role))
    return text

def build_prompt_parts(role: str | None = None) -> tuple[str, str]:
    """返回 (稳定块, 动态块)。稳定块每轮不变，放前面吃前缀缓存；动态块只有场景状态。"""
    stable: list[str] = []

    prompt_text = services.prompts().read_text(services.prompts().PROMPT_FILE).strip()
    if prompt_text:
        stable.append(prompt_text)

    # Optional run-level layer: how to write, not who to write. Missing file is fine.
    inject_text = services.prompts().read_text(services.prompts().INJECT_FILE).strip()
    if inject_text:
        stable.append(inject_text)

    memory_text = services.prompts().read_text(services.prompts().MEMORY_MD).strip()
    if role:
        # One actor only ever reads the shared archive plus her own file.
        memory_text = (memory_text + "\n\n" + services.prompts().read_text(services.prompts().personal_memory(role)).strip()).strip()
    if services.world().shared_world_data().get('memory_reset_at'):
        # The archive is reference material and survives a reset intact; only chat history is cleared.
        stable.append('这是已重置的新故事，从当前现实时间重新开始。数据库里的旧聊天、场景和关系进展不作为已经发生过的事；'
                      '角色档案里的性格、身体设定、关系背景和共同经历照旧有效，可以当作她记得的事来引用。')
    from backend.app.characters.context_budget import topic,select_profile
    if topic.get() is not None:
        shared=services.prompts().read_text(services.prompts().MEMORY_MD)
        own=services.prompts().read_text(services.prompts().personal_memory(role)) if role else ''
        memory_text=select_profile(shared,topic.get(),1000)+'\n'+select_profile(own,topic.get(),1500)
    if memory_text:
        stable.append("# 角色档案与记忆\n" + memory_text)

    dynamic: list[str] = []
    state = services.world().model_scene()
    if state:
        dynamic.append("# 当前场景状态\n" + json.dumps(state, ensure_ascii=False, indent=2))

    s = "\n\n".join(stable) if stable else "按系统指定角色自然回应当前消息。"
    d = "\n\n".join(dynamic)
    return s, d

def build_system_prompt(role: str | None = None) -> str:
    """兼容旧调用：稳定块 + 动态块拼在一起。"""
    s, d = services.prompts().build_prompt_parts(role)
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

def shared_world_data():
    world = chat_world.get()
    identity = world['id'] if world else storage.scope.get()[1]
    snapshot = storage.request_snapshot.get()
    if world and world.get('read_only') and snapshot is not None and snapshot['scope'] == (storage.scope.get()[0], identity):
        return snapshot['data']
    if world and world.get('read_only') and 'data' in world:
        return world['data']
    return storage.get_store().read(storage.scope.get()[0], identity)

def displayed_scene():
    """All windows show the same live world and actor states."""
    data = services.world().shared_world_data()
    state = dict(data.get('scene_state', {}))
    if data.get('realtime_clock'):state['时间']=data['realtime_clock']['minute']
    for role, value in data.get('role_states', {}).items():
        if role in services.world().PRIVATE_NAMES:
            state[services.world().PRIVATE_NAMES[role]] = value.get('status', state.get(services.world().PRIVATE_NAMES[role], '未知'))
    for name in services.world().PRIVATE_NAMES.values():
        if name in state:state[name]=scene_labels.label(state[name])
    return state

def model_scene():
    """Shared public facts plus this actor's own state; private facts aren't omniscient."""
    data = services.world().shared_world_data()
    state = dict(data.get('scene_state', {}))
    if data.get('realtime_clock'):state['时间']=data['realtime_clock']['minute']
    world = chat_world.get()
    role = world['channel'] if world else 'group'
    own = data.get('role_states', {}).get(role, {})
    if role in services.world().PRIVATE_NAMES and own.get('status'):
        state[services.world().PRIVATE_NAMES[role]] = own['status']
    return state

def role_avatars():
    world = chat_world.get()
    identity = world["id"] if world else storage.scope.get()[1]
    items = storage.get_store().conversations(storage.scope.get()[0])
    story = next((item for item in items if item["id"] == identity), {})
    from backend.app.characters import avatar_assets
    return {name: avatar_assets.display(channel,story.get("channels", {}).get(channel, {}).get("avatar", "")) for channel, name in services.world().PRIVATE_NAMES.items()}

def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "•" * 6 + key[-4:]

def story_root_id() -> str:
    world = chat_world.get()
    return world['id'] if world else storage.scope.get()[1]

def read_story_flag(name: str, default=False):
    """Story-wide switches live on the root conversation so all four channels agree."""
    user = storage.scope.get()[0]
    try:
        data = storage.get_store().read(user, services.world().story_root_id())
    except Exception:
        return default
    return data.get(name, default)

def write_story_flag(name: str, value) -> None:
    user = storage.scope.get()[0]
    storage.get_store().mutate(user, services.world().story_root_id(), lambda data: data.update({name: value}))

def friendly_error(exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc)
    if "data_inspection" in msg or "inappropriate" in msg:
        side = "输入" if "Input" in msg else "输出"
        msg += (
            f"\n\n供应商的{side}内容审核拦的，不是代码问题。"
            "降审核层只能压低倾向，压不住这种硬性过滤。"
            "要真放开只能换不审内容的模型：本地 Ollama 最省事，"
            "或者在 config/model.json 里换成别家接口。"
        )
    elif status == 400:
        hint = "上游拒绝了请求参数，不是网络问题。"
        low = text.lower()
        if "model" in low and ("support" in low or "not exist" in low or "invalid" in low):
            hint += "\n模型名与接口地址不匹配：这个 base_url 上没有这个模型。常见组合见设置页——DeepSeek 官方用 deepseek-flash，本地 Ollama 用 qwen2.5:14b 一类。"
        elif "max_tokens" in low or "max_completion_tokens" in low:
            hint += "\n这个模型不收 max_tokens，改用 max_completion_tokens，或把「回复长度上限」留空。"
        elif "temperature" in low:
            hint += "\n推理型模型通常只接受 temperature=1，把回复随机性改成 1。"
        elif "embedding_model" in low or "unsupported parameter" in low:
            hint += "\n请求里带了该接口不认的字段，去掉后重试。"
        else:
            hint += "\n把模型名、接口地址、参数按设置页的提示对齐后重试。"
        msg += "\n\n" + hint
    elif status == 404:
        msg += "\n\n接口路径不存在：base_url 通常要以 /v1 结尾，或去掉了 /v1。"
    elif status in (401, 403):
        msg += "\n\n密钥无效或没有该模型权限：检查 API Key 是否属于这个接口地址。"
    elif status == 429:
        msg += "\n\n限流或额度用尽，等一会儿再试。"
    return msg

def make_client(cfg: dict) -> AsyncOpenAI:
    from backend.app.infrastructure.model_transport import client_options
    return AsyncOpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"] or None,
        timeout=cfg.get("timeout", 120),
        **client_options(cfg.get('base_url'),asynchronous=True),
    )

def make_kwargs(cfg: dict, body: "ChatIn") -> dict:
    kw: dict[str, Any] = dict(
        model=cfg["model"],
        temperature=body.temperature if body.temperature is not None else cfg["temperature"],
    )
    limit = cfg.get("max_tokens")
    if isinstance(limit, int) and limit > 0:   # 0 / null 表示不限制
        kw["max_tokens"] = limit
    from backend.app.mcp.access import budget_client
    if budget_client():kw['max_tokens']=min(kw.get('max_tokens') or 2048,2048)
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
    world = {**world, 'data': services.world().shared_world_data()}
    with actors.role_scope(user, world["id"], role):
        cfg = services.models().load_config()
        persona = storage.read_document("persona", "")
        relationship = storage.read_document("relationship", "")
        own_state=world['data'].get('role_states',{}).get(role,{})
        name = services.world().PRIVATE_NAMES[role]
        from backend.app.characters import context_budget
        query=role_memory.query_for(messages)
        selection=context_budget.topic.set(query)
        try:stable, _ = services.prompts().build_prompt_parts(role)
        finally:context_budget.topic.reset(selection)
        current_context=context_budget.dialogue_window(messages)
        receipt_fact=role_actions.received_contact_status(user,world['id'],role,
            next((m.get('content','') for m in reversed(messages) if m['role']=='user'),''))
        query=role_memory.query_for(messages)
        memory=role_memory.remember(user,world['id'],role,query,group,
            exclude_ids=[m['id'] for m in current_context if m.get('id')],
            exclude_texts=[m.get('content','') for m in current_context]) if context_budget.needs_recall(query) else {'events':[]}
        from backend.app.world import actor_context
        facts,history,communication=actor_context.build(user,world['id'],role,query,current_context)
        payload=[{'role':'system','content':stable},
            {'role':'system','content':'当前事实与明确设定：'+json.dumps(facts,ensure_ascii=False,separators=(',',':'))+
             '\n时间以当前事实为准。stale仅表示最后观察可能过时，不证明活动完成或位置改变；未知保持未知。'},
            {'role':'system','content':role_memory.render(memory) if memory['events'] else '当前无需额外历史检索，以近期原文及明确设定为准。'}]
        if history:
            payload.append({'role':'system','content':'本轮相关历史证据（历史动作不等于当前仍在进行；听说不等于亲历）：'+json.dumps(history,ensure_ascii=False,separators=(',',':'))})
        if communication:
            payload.append({'role':'system','content':'真实通信执行结果（sent仅证明投递，不证明已读、回复或新请求已执行）：'+json.dumps(communication,ensure_ascii=False,separators=(',',':'))})
        if receipt_fact:payload.append({'role':'system','content':'已核实的通信事实，用本人自然口吻回答：'+receipt_fact})
        payload += [{'role':m['role'],'content':m['content']} for m in current_context]
        from backend.app.characters import actor_instructions
        payload.append({'role':'system','content':actor_instructions.render(name,group,persona,relationship)})
        used=services.prompts().recent_phrases(current_context,name)
        if used:
            payload.append({'role':'system','content':
                '你最近几轮已经说过的话：「'+'」「'.join(used)+'」。'
                '本轮一个字都不许重复这些句子，也不许换个语气把同一句再说一遍；'
                '换新的动作、新的身体感受、新的内容，不要用口头禅填满回复。'})
        return cfg, context_budget.fit(payload)

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
    old_clock=world_time.clock(services.world().shared_world_data())
    next_clock=world_time.advance_candidate(history,old_clock)
    # Validate configuration without building three unused memory prompts.
    prepared = {}
    def prepare_config(role):
        with actors.role_scope(storage.scope.get()[0],world['id'],role):return services.models().load_config()
    for role in targets:
        cfg = await asyncio.to_thread(prepare_config,role)
        prepared[role] = (cfg, [])
        problem = services.models().check_cfg(cfg)
        if problem:
            return JSONResponse({"error":services.world().PRIVATE_NAMES[role] + "：" + problem},400)
    try:
        claimed = await asyncio.to_thread(chat_history.claim,body.request_id,chat_inbox.processing.get())
    except RuntimeError as exc:
        return JSONResponse({"error":str(exc)},409)
    if not claimed:
        async def replay():
            yield services.generation().sse({"done":True,"messages":await asyncio.to_thread(chat_history.visible_messages),"replayed":True})
        return StreamingResponse(replay(),media_type="text/event-stream")
    try:
        await asyncio.to_thread(chat_history.append,body.request_id,0,"user",current)
        records=await asyncio.to_thread(chat_history.messages)
        user_record=next(m for m in records if m['id']==chat_history.identity(storage.scope.get()[1],body.request_id,0))
        await asyncio.to_thread(world_events.capture,storage.scope.get()[0],world['id'],world['channel'],user_record)
        if next_clock!=old_clock:
            latest=await asyncio.to_thread(chat_history.messages,1)
            active={world['channel']} if not group else set()
            from backend.app.world import state_events
            await asyncio.to_thread(state_events.advance_story,storage.scope.get()[0],world['id'],
                next_clock,active,latest[-1]['created_at'])
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
                name = services.world().PRIVATE_NAMES[role]
                yield services.generation().sse({"status": "正在等待群聊回应…" if group else name + "正在回复…", "speaker":name})
                cfg, payload = await asyncio.to_thread(services.generation().actor_payload,role,history,group)
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
                        if cross in services.world().PRIVATE_NAMES:
                            await asyncio.to_thread(peer_chat.queue,storage.scope.get()[0],world['id'],role,
                                'followup',action['id'],{'channel':world['channel'],'source':action['id'],
                                'question':current[-900:],'message_id':action['detail']['message_id'],
                                'created_at':__import__('time').time()},5)
                        from backend.app.characters import action_voice
                        if action['detail'].get('delivery_report'):
                            try:
                                voiced=await action_voice.phrase(role,cfg,action['detail']['delivery_report'],history,group)
                                response=voiced
                            except Exception:
                                response='我转告过她了。'
                        else:
                            response=action['detail'].get('sent_reply') or action['detail'].get('reply','')
                            if not action_voice.natural(response):response='嗯，跟'+services.world().PRIVATE_NAMES[cross]+'说了。' if cross in services.world().PRIVATE_NAMES else '嗯，发了。'
                    elif state=='pending':
                        response=action['detail'].get('reply') or '等我忙完这会儿再说。'
                    elif state=='failed':
                        response='刚才没发出去，我再看看。'
                    else:
                        response=action['detail'].get('reply') or ('这次我不想替你发。' if state=='refuse' else '你想让我具体发什么？')
                        if world_events.fake_delivery(response):response='我这次还没有实际发送。'+('这条我不想替你发。' if state=='refuse' else '我需要确认要发的内容。')
                    text=actors.normalize_reply(response,name)
                    yield services.generation().sse({'delta':('\n\n' if completed else '')+text})
                    yield services.generation().sse({'action':{'id':action['id'],'status':state,'target':cross}})
                else:
                    client = services.models().make_client(cfg)
                    raw = ""
                    interruption_check=0
                    prefix = "\n\n" if completed else ""
                    try:
                        with model_usage.Measure('character',cfg,payload) as metric:
                            options=services.models().make_kwargs(cfg,body)
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
                                        yield services.generation().sse({'delta':prefix+piece,'speaker':name})
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
                        from backend.app.characters import reply_quality
                        text=reply_quality.trim_repeated(text,history)
                        if not text:continue
                        if reply_quality.redundant(text,completed):
                            break
                        yield services.generation().sse({'delta':prefix+text,'speaker':name})
                if not action:
                    latest_history=await asyncio.to_thread(chat_history.messages)
                    if message_service.cancels_draft(latest_history,user_record['created_at']):
                        break  # Discard unsent stale model text; next turn uses the interruption.
                metadata={'source':'role_action','action_id':action['id']} if action else None
                await asyncio.to_thread(chat_history.append,body.request_id,index,"assistant",text,name,True,metadata)
                completed.append(text)
                history.append({"id":chat_history.identity(storage.scope.get()[1],body.request_id,index),"role":"assistant","content":text})
                yield services.generation().sse({"replace":"\n\n".join(completed)})
                saved = await asyncio.to_thread(chat_history.messages,1)
                if saved:
                    await asyncio.to_thread(world_events.capture,storage.scope.get()[0],world['id'],world['channel'],saved[-1])
                    yield services.generation().sse({"saved_message":saved[-1]})
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
                    services.generation().wake_maintenance()
            except Exception:
                yield services.generation().sse({'warning':'回复已保存，后台维护排队失败；聊天记录不受影响。'})
            yield services.generation().sse({"done":True,"no_reply":group and not completed,"maintenance_pending":queued,
                "messages":await asyncio.to_thread(chat_history.visible_messages)})

        except Exception as exc:
            yield services.generation().sse({"replace":"\n\n".join(completed)})
            yield services.generation().sse({"error":services.models().friendly_error(exc),"messages":await asyncio.to_thread(chat_history.visible_messages)})
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
    from backend.app.world.scene_updates import update_scene
    return update_scene(messages,force,cfg_override)


def scene_brief() -> str:
    """把记忆包里的场景状态压成几行，给「描述」当上下文。"""
    try:
        pack = storage.memory_pack()
    except json.JSONDecodeError:
        return ""
    state = services.world().model_scene()
    if not state:
        return ""
    lines = []
    for k, v in state.items():
        if isinstance(v, (str, int, float)):
            lines.append(f"{k}：{v}")
    return "\n".join(lines)

async def chat(body: ChatIn):
    response = await services.generation().chat_stream(body)
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
