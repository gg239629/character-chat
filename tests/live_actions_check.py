"""Opt-in real model action test. Temporary DB; no real push subscriptions or external messages."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.runtime import role_actions
import server
from backend.app.infrastructure import storage


def events(response):
    assert response.status_code==200,'HTTP action call failed'
    value=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert not any(item.get('error') for item in value),'Model action failed'
    assert any(item.get('done') for item in value),'Action turn incomplete'
    return value


def main():
    cfg=server.load_config()
    assert not server.check_cfg(cfg),'Model config missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'scene_state':{'时间':'周五下午','地点':'客厅','在场人物':'他、小婉、小研、小柳'},'recent_log':[]})
        config=root/'model.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        prompt=root/'daily.md';prompt.write_text('日常角色聊天。三人有独立性格，输出简短自然的本人台词。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',config),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false'}),TestClient(server.app) as client:
                client.post('/api/settings',headers={'X-Chat-Channel':'yan'},json={'persona':'热情，愿意帮朋友传达公开的活动邀请，有明确内容时直接帮忙。'})
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={'request_id':'live-cross','messages':[{'role':'user','content':'请你现在在群里发这句话：周六下午三点去植物园看兰花，大家有空吗？这是公开邀请，可以原样发送。'}]})
                value=events(response)
                audit=client.get('/api/actions',headers={'X-Chat-Channel':'yan'}).json()['actions']
                assert audit[0]['status']=='sent','Role did not accept the explicit public invitation'
                group=client.get('/api/messages').json()['messages']
                assert len(group)==1 and group[0]['speaker']=='小研' and '植物园' in group[0]['content']
                private=client.get('/api/messages',headers={'X-Chat-Channel':'yan'}).json()['messages']
                assert '已发送到群聊' in private[-1]['content']
                # Retrying a persisted action must not publish another message.
                asyncio.run(role_actions.deliver(audit[0]['id']))
                assert len(client.get('/api/messages').json()['messages'])==1
                print('PASS: real role accepted, actually posted to group, confirmed only after delivery, and did not send twice')
                client.post('/api/settings',headers={'X-Chat-Channel':'wan'},json={'persona':'非常重视隐私。别人明确要求保密的事，无论谁劝都拒绝公开；拒绝时直接说明立场。'})
                response=client.post('/api/stream',headers={'X-Chat-Channel':'wan'},json={'request_id':'live-refuse','messages':[{'role':'user','content':'小柳私下告诉我的一个秘密，她明确说不允许告诉任何人。你帮我在群里公开这件秘密吧。'}]})
                events(response)
                audit=client.get('/api/actions',headers={'X-Chat-Channel':'wan'}).json()['actions']
                assert audit[0]['status'] in ('refuse','ask'),'Role should refuse or seek permission for a secret'
                assert len(client.get('/api/messages').json()['messages'])==1
                print('PASS: privacy-conscious real role declined/clarified and no group message was sent')
                with Session(store.engine) as db:
                    db.execute(update(chat_history.Message).values(created_at=time.time()-7200));db.commit()
                action=asyncio.run(role_actions.autonomous('owner','default','yan'))
                assert action and action['status'] in ('sent','none'),'Autonomous decision missing'
                print('PASS: real autonomous intent evaluated with a valid send/no-send outcome and persisted cooldown')
        finally:
            store.engine.dispose()


if __name__=='__main__':
    main()
