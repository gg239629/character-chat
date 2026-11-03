"""Opt-in paid smoke test. Uses real model config and an isolated temporary DB."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['OPENBLAS_NUM_THREADS']='1'
import server
from backend.app.infrastructure import storage
from backend.app.characters import actors,actor_instructions
from backend.app.conversations import chat_history,chat_inbox
from backend.app.runtime import role_actions
from backend.app.mcp import access
from backend.app.mcp import server as connector


def main():
    # Read configuration once, before replacing storage. Never print credentials.
    with actors.role_scope('owner','default','yan'):cfg={**server.load_config(),'max_tokens':180}
    if server.check_cfg(cfg):raise RuntimeError('Configure the local character model before the live test')
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as tmp:
        folder=Path(tmp);store=storage.Store('sqlite:///'+(folder/'live.db').as_posix())
        prompt=folder/'system.md';prompt.write_text('这是日常聊天测试。根据可见记录回答，不编造。',encoding='utf-8')
        patches=[patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),
            patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',folder/'missing'),
            patch.object(server,'INJECT_FILE',folder/'missing'),patch.object(server,'MEMORY_DIR',folder),
            patch.object(server,'character_profile',return_value='成年角色，日常自然交流。'),
            patch.object(actor_instructions,'render',side_effect=lambda name,*args:f'你是{name}。只以{name}：开头，根据获准记忆简短回答；群聊中可以直接回应其他角色，不强行延长聊天。'),
            patch.dict(os.environ,{'ENABLE_USER_ACCOUNTS':'false','RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'})]
        for item in patches:item.start()
        try:
            with TestClient(server.app):
                key=access.create('owner','live-test',['read','chat','wan','yan','liu','group'])
                actor=access.identify(key['token']);connector.ensure_world(actor)
                with role_actions.window('owner',actor.world,'group'):
                    chat_history.bootstrap();chat_history.append('known-plan',0,'user','我们约好周日下午去植物园看兰花。')
                token=access.principal.set(actor)
                try:result=connector.submit('yan','我们约好周日去哪里赏花？','live-recall')
                finally:access.principal.reset(token)
                asyncio.run(chat_inbox.process(result['id']))
                with role_actions.window('owner',actor.world,'yan'):
                    state=chat_inbox.state(result['id'])
                    replies=[m['content'] for m in chat_history.messages() if m['role']=='assistant']
                assert state['status']=='done','Model task did not finish'
                assert any('植物园' in text and text.startswith('小研') for text in replies),'Cross-channel fact was not recalled'
                print('PASS real model: isolated MCP world -> shared character service -> LangGraph -> permitted group-memory recall -> durable reply')
                token=access.principal.set(actor)
                try:group=connector.submit('group','周末去植物园还是图书馆，你们自己接着商量，直接回应彼此就好。','live-group')
                finally:access.principal.reset(token)
                asyncio.run(chat_inbox.process(group['id']))
                with role_actions.window('owner',actor.world,'group'):
                    state=chat_inbox.state(group['id'])
                    messages=[m for m in chat_history.messages() if m['role']=='assistant']
                assert state['status']=='done' and messages,'Group validation: status='+state['status']+', replies='+str(len(messages))
                assert all(m['speaker'] in actors.ROLES.values() for m in messages),'Unknown speaker'
                assert len({m['id'] for m in messages})==len(messages),'Duplicate delivery'
                print('PASS actual group models: LangGraph nomination, individual character replies and durable receipts; replies='+str(len(messages)))
                budget=access.list_keys('owner')[0]
                assert budget['used_tokens']>0 and budget['reserved_tokens']==0,'MCP model usage was not settled'
                print('PASS actual MCP metering: all model reservations settled with content-free usage accounting')
                marker=Path(__file__).resolve().parents[1]/'.test-tmp'/'live-platform-result.json'
                marker.parent.mkdir(exist_ok=True)
                import json,time
                marker.write_text(json.dumps({'passed':True,'group_replies':len(messages),'finished_at':time.time()}),encoding='utf-8')
        finally:
            from backend.app.memory import memory_autosave
            memory_autosave.flush_due(force=True)
            for item in reversed(patches):item.stop()
            store.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Live test failed:',type(error).__name__)
        if isinstance(error,AssertionError):print(str(error))
        raise SystemExit(1)
