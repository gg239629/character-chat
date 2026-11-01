"""Actual model interruption test using a disposable story and no live chat data."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as S
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import storage
import server
import actors
import chat_inbox
import chat_history
import role_actions


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'scene_state':{},'recent_log':[]})
        prompt=root/'system.md';prompt.write_text('日常微信群聊天。仅扮演本人，简短自然回复。用户最新明确纠正优先于旧提议。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),\
                patch.object(server,'PROMPT_FILE',prompt),\
                patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{
                    'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                async def scenario():
                    with role_actions.window('owner','default','group'):
                        first=chat_inbox.accept(server.ChatIn(request_id='first-topic',messages=[{
                            'role':'user','content':'小研，周末去科学馆吧，请你先说说想法。'}]))
                    original=server.make_client;second=None;interrupted=False
                    def make(cfg):
                        real=original(cfg)
                        async def create(**kwargs):
                            nonlocal second,interrupted
                            result=await real.chat.completions.create(**kwargs)
                            if not kwargs.get('stream'):return result
                            async def chunks():
                                nonlocal second,interrupted
                                async for chunk in result:
                                    if not interrupted and chunk.choices and chunk.choices[0].delta.content:
                                        interrupted=True
                                        # Execute at the moment a real model is replying.
                                        with role_actions.window('owner','default','group'):
                                            second=chat_inbox.accept(server.ChatIn(request_id='interruption',messages=[{
                                                'role':'user','content':'小研，先别聊科学馆了，改去蓝桥公园。请简短确认最终地点是哪里。'}]))
                                    yield chunk
                            return chunks()
                        return S(chat=S(completions=S(create=create)),close=real.close)
                    with patch.object(server,'make_client',side_effect=make):
                        await chat_inbox.process(first['id'])
                        assert interrupted and second,'No active-model interruption occurred'
                        with role_actions.window('owner','default','group'):
                            assert len(chat_history.messages())==2,'Unsent stale answer was published'
                        await chat_inbox.process(second['id'])
                    with role_actions.window('owner','default','group'):
                        messages=chat_history.messages()
                        replies=[m for m in messages if m['role']=='assistant']
                        assert replies and any('蓝桥公园' in m['content'] for m in replies),'Actual reply missed latest interruption'
                        assert len([m for m in messages if m['role']=='user'])==2,'User messages lost or duplicated'
                    print('PASS: accepted interruption during real group generation, discarded stale unsent answer, responded to latest topic, preserved both user messages')
                asyncio.run(scenario())
        finally:store.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('FAIL:',type(exc).__name__)
        if isinstance(exc,AssertionError):print(str(exc))
        raise SystemExit(1)
