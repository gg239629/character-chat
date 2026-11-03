"""Official MCP SDK transport. Website and MCP use the same character service."""
import asyncio
import json
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from backend.app.mcp import access
from backend.app.characters import actors
from backend.app.characters.service import characters
from backend.app.infrastructure import storage
from backend.app.world.realtime_world import fresh_scene
from backend.app.conversations import chat_inbox
from sqlalchemy.orm import Session

mcp=FastMCP('Character Chat',log_level='WARNING',instructions='Three independent fictional characters. Calls are isolated from the website story and other clients.',
    streamable_http_path='/',stateless_http=True,json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


def caller(operation=None,channel=None):
    actor=access.principal.get()
    if actor is None:raise PermissionError('Authentication required')
    if operation:actor.require(operation,channel)
    return actor


def ensure_world(actor):
    storage.get_store().ensure(actor.user_id,actor.world,{'title':'外部人物会话','origin':'mcp','recent_log':[],
        'initiative_enabled':False,'mcp_client_id':actor.client_id,**fresh_scene()})
    if not storage.get_store().read(actor.user_id,actor.world).get('mcp_client_id'):
        storage.get_store().mutate(actor.user_id,actor.world,lambda data:data.update(mcp_client_id=actor.client_id))


def submit(channel,message,request_id):
    actor=caller('chat',channel)
    if not isinstance(message,str) or not message.strip() or len(message)>10000:raise ValueError('message must contain 1–10000 characters')
    import re
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',request_id):raise ValueError('request_id must contain 1–64 letters, digits, underscores or hyphens')
    ensure_world(actor)
    # Retries do not consume another request credit. Acceptance is idempotent.
    import hashlib
    cid=actor.world if channel=='group' else actors.role_id(actor.world,channel)
    identity=hashlib.sha256(f'{actor.user_id}:{cid}:{request_id}'.encode()).hexdigest()
    with Session(storage.get_store().engine) as db:existing=db.get(chat_inbox.Turn,identity)
    if existing is None:access.reserve(actor,identity)
    return {**characters.send(actor.user_id,actor.world,channel,message,request_id),'channel':channel}


@mcp.tool()
def list_characters() -> list[dict]:
    """List public character cards; no personal history or system prompts."""
    actor=caller()
    descriptions={'wan':'开朗直接，喜欢热闹','yan':'温柔细致，容易亲近','liu':'独立自信，表达干脆'}
    return [{'id':role,'name':name,'description':descriptions[role]} for role,name in actors.ROLES.items() if role in actor.scopes]


@mcp.tool()
async def chat_with_character(character:str,message:str,request_id:str) -> dict:
    """Queue a message to wan, yan or liu. Reuse request_id on retry. Poll get_run for completion."""
    if character not in actors.ROLES:raise ValueError('Invalid character')
    return await asyncio.to_thread(submit,character,message,request_id)


@mcp.tool()
async def chat_with_group(message:str,request_id:str) -> dict:
    """Queue a group message in this client's isolated world. No fixed speaker count."""
    return await asyncio.to_thread(submit,'group',message,request_id)


@mcp.tool()
def get_run(run_id:str,channel:str) -> dict:
    """Get a task status in its original channel; cannot inspect another client's task."""
    actor=caller('read',channel)
    return characters.run(actor.user_id,actor.world,channel,run_id)


@mcp.tool()
def read_messages(channel:str,limit:int=30) -> list[dict]:
    """Read up to 100 visible messages in this client's own group or character DM. Never peer DMs."""
    actor=caller('read',channel);ensure_world(actor)
    return characters.messages(actor.user_id,actor.world,channel,limit)


@mcp.resource('characters://cards')
def character_cards() -> str:
    """Public personality cards of permitted characters."""
    return json.dumps(list_characters(),ensure_ascii=False)


class BearerAuth:
    """No cookie authentication on MCP; every HTTP message revalidates the token.

    This initial connector uses explicit Bearer credentials, not OAuth discovery.
    Cross-origin browser requests are rejected; desktop/service clients omit Origin.
    """
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=dict(scope.get('headers',[]))
        origin=headers.get(b'origin',b'').decode()
        host=headers.get(b'host',b'').decode()
        if origin and origin.rstrip('/') not in ('https://'+host,'http://'+host):
            return await JSONResponse({'error':'Origin not allowed'},403)(scope,receive,send)
        authorization=headers.get(b'authorization',b'').decode()
        value=authorization[7:] if authorization.startswith('Bearer ') else ''
        actor=await asyncio.to_thread(access.identify,value)
        if actor is None:return await JSONResponse({'error':'MCP bearer credential required'},401,headers={'WWW-Authenticate':'Bearer'})(scope,receive,send)
        token=access.principal.set(actor)
        try:await self.app(scope,receive,send)
        finally:access.principal.reset(token)


class Host:
    def __init__(self):self.instances={}
    async def start(self):
        loop=asyncio.get_running_loop()
        if loop in self.instances:
            self.instances[loop]['users']+=1;return
        server=FastMCP('Character Chat',log_level='WARNING',streamable_http_path='/',stateless_http=True,json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        for tool in (list_characters,chat_with_character,chat_with_group,get_run,read_messages):server.add_tool(tool)
        server.resource('characters://cards')(character_cards)
        ready=asyncio.Event();stop=asyncio.Event()
        async def lifetime():
            async with server.session_manager.run():
                ready.set();await stop.wait()
        task=asyncio.create_task(lifetime())
        self.instances[loop]={'app':BearerAuth(server.streamable_http_app()),'stop':stop,'task':task,'users':1}
        await ready.wait()
    async def stop(self):
        loop=asyncio.get_running_loop();state=self.instances.get(loop)
        if not state:return
        state['users']-=1
        if state['users']:return
        state['stop'].set();await state['task'];self.instances.pop(loop,None)
    async def __call__(self,scope,receive,send):
        state=self.instances.get(asyncio.get_running_loop())
        if not state:return await JSONResponse({'error':'MCP starting'},503)(scope,receive,send)
        return await state['app'](scope,receive,send)


host=Host()
