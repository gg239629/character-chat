"""One durable execution graph for website and MCP turns.

Checkpoint state contains identifiers/status only. Canonical messages, delivery
receipts and task leases remain transactional in the application database.
"""
import asyncio
import hashlib
import threading
from contextvars import ContextVar
from sqlalchemy import event
from weakref import WeakKeyDictionary
from contextlib import asynccontextmanager
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.orm import Session
from backend.app.infrastructure import storage


class AsyncSaver(BaseCheckpointSaver):
    """Offload official synchronous checkpoint stores from the event loop."""
    def __init__(self,saver):
        super().__init__(serde=saver.serde);self.saver=saver
    async def aget_tuple(self,config):return await asyncio.to_thread(self.saver.get_tuple,config)
    async def aput(self,*args,**kwargs):return await asyncio.to_thread(self.saver.put,*args,**kwargs)
    async def aput_writes(self,*args,**kwargs):return await asyncio.to_thread(self.saver.put_writes,*args,**kwargs)
    async def alist(self,*args,**kwargs):
        for item in await asyncio.to_thread(lambda:list(self.saver.list(*args,**kwargs))):yield item


_resources=WeakKeyDictionary()
_resource_lock=threading.RLock()
_runner=ContextVar('graph_runner')


def _resource(engine):
    with _resource_lock:
        if engine in _resources:return _resources[engine]
        url=engine.url
        if url.get_backend_name()=='sqlite':
            from langgraph.checkpoint.sqlite import SqliteSaver
            path=url.database
            manager=SqliteSaver.from_conn_string(path+'.checkpoints' if path and path!=':memory:' else ':memory:')
            saver=manager.__enter__()
            def close():
                # Fold WAL sidecars before releasing the last app-owned handle.
                # This also prevents a Windows directory-removal race at teardown.
                import sqlite3
                with saver.lock:
                    try:
                        cursor=saver.conn.cursor()
                        try:
                            cursor.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                            cursor.execute('PRAGMA journal_mode=DELETE')
                        finally:cursor.close()
                    except sqlite3.Error:pass  # Another process may still own a reader.
                    finally:manager.__exit__(None,None,None)
        else:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
            pool=ConnectionPool(url.set(drivername='postgresql').render_as_string(hide_password=False),min_size=1,max_size=4,
                kwargs={'autocommit':True,'prepare_threshold':0,'row_factory':dict_row})
            saver=PostgresSaver(pool);close=pool.close
        try:saver.setup()
        except BaseException:close();raise
        resource={'saver':AsyncSaver(saver),'close':close,'graph':None}
        _resources[engine]=resource
        def disposed(engine):
            with _resource_lock:old=_resources.pop(engine,None)
            if old:old['close']()
        event.listen(engine,'engine_disposed',disposed)
        return resource


@asynccontextmanager
async def checkpoint_store():
    resource=await asyncio.to_thread(_resource,storage.get_store().engine)
    yield resource['saver']


def close_resources():
    with _resource_lock:
        resources=list(_resources.values());_resources.clear()
    for resource in resources:resource['close']()


def snapshot(identity):
    from backend.app.conversations.chat_inbox import Turn
    with Session(storage.get_store().engine) as db:
        row=db.get(Turn,identity)
        if not row:return None
        return {'status':row.status,'thread':hashlib.sha256((row.user_id+':'+row.world_id+':'+row.channel+':'+row.id).encode()).hexdigest()}


async def validate(state):
    value=await asyncio.to_thread(snapshot,state['run_id'])
    return {'status':value['status'] if value else 'missing','stage':'validated'}


async def generate(state):
    await _runner.get()(state['run_id'])
    value=await asyncio.to_thread(snapshot,state['run_id'])
    return {'status':value['status'] if value else 'missing','stage':'completed'}


def compiled(resource):
    with _resource_lock:
        if resource['graph'] is not None:return resource['graph']
        builder=StateGraph(TurnState)
        builder.add_node('validate',validate);builder.add_node('character_exchange',generate)
        builder.add_edge(START,'validate')
        builder.add_conditional_edges('validate',lambda state:'stop' if state['status'] in ('missing','done','superseded','failed') else 'run',{'stop':END,'run':'character_exchange'})
        builder.add_edge('character_exchange',END)
        resource['graph']=builder.compile(checkpointer=resource['saver'])
        return resource['graph']


class TurnState(TypedDict,total=False):
    run_id:str
    status:str
    stage:str


async def execute(identity,runner):
    initial=await asyncio.to_thread(snapshot,identity)
    if not initial or initial['status'] in ('done','superseded','failed'):return
    resource=await asyncio.to_thread(_resource,storage.get_store().engine)
    graph=compiled(resource)
    config={'configurable':{'thread_id':initial['thread']},'recursion_limit':8}
    token=_runner.set(runner)
    try:
        previous=await graph.aget_state(config)
        await graph.ainvoke(None if previous.next else {'run_id':identity},config)
    finally:_runner.reset(token)
