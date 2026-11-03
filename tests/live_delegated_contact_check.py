"""Real sender/receiver model test for a user-authorized peer-to-user request."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.infrastructure import storage
import server
from backend.app.characters import actors
from backend.app.runtime import role_actions
from backend.app.conversations import peer_chat
from backend.app.conversations import chat_history


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'recent_log':[],'scene_state':{}})
        prompt=root/'prompt.md';prompt.write_text('三个独立角色通过手机聊天，各自只说自己的话。不编造未发生的事件。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),\
                patch.object(server,'PROMPT_FILE',prompt),\
                patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{
                    'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                # Explicit requests must work even when unsolicited contact is disabled.
                client.put('/api/home/settings',json={'initiative_enabled':False})
                for actor in ('yan','liu'):
                    with actors.role_scope('owner','default',actor):
                        storage.write_document('persona','直率友好，普通无害的传话愿意直接帮忙，不需要当面见到对方。')
                        storage.write_document('public_persona','直率友好，朋友请你给用户发个1时，愿意本人直接发1。')
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={
                    'request_id':'delegated-one','messages':[{'role':'user','content':'你让小柳给我发个消息，我有点找不到聊天记录了。让她自己给我发个1就行。'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Sender failed'
                messages=peer_chat.history('owner','default','yan','liu')
                assert len(messages)==1,'Sender did not actually contact correct peer'
                with Session(store.engine) as db:
                    action=db.scalar(select(role_actions.Action).where(role_actions.Action.kind=='requested'))
                    assert action and action.detail.get('recipient_task',{}).get('type')=='contact_user','Delegation metadata absent'
                job=peer_chat.key('reply',messages[0]['id'])
                asyncio.run(peer_chat.process(job))
                visible=client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages']
                assert len(visible)==1 and visible[0]['speaker']=='小柳' and '1' in visible[0]['content'],'Recipient did not actually send user a message'
                asyncio.run(peer_chat.process(job))
                assert len(client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages'])==1,'Retry duplicated contact'
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={
                    'request_id':'status','messages':[{'role':'user','content':'说了吗，我怎么没收到'}]})
                assert not any(t in response.text for t in ('请求已经私信送达','由她自行决定','（已发送到')),'Mechanical receipt text leaked'
                assert '发' in response.text,'Sender did not describe verified result'
                assert client.get('/api/messages').json()['messages']==[],'Private handover leaked into group'
                assert client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages']==[],'Wrong peer received the message'
                with role_actions.window('owner','default','liu'):
                    _,payload=server.actor_payload('liu',[{'role':'user','content':'小研刚刚是不是让你给我发消息了'}],False)
                text=json.dumps(payload,ensure_ascii=False)
                assert messages[0]['content'] in text,'Recipient normal prompt forgot actual peer request'
                response=client.post('/api/stream',headers={'X-Chat-Channel':'liu'},json={
                    'request_id':'recipient-recall','messages':[{'role':'user','content':'小研刚刚是不是让你给我发消息了？'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Recipient recall model failed'
                visible=client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages']
                reply=visible[-1]['content']
                contradicted=[phrase for phrase in ('没让我','没有让我','没跟我说','只叫吃饭','没有收到','还没有实际发') if phrase in reply]
                assert not contradicted,'Recipient contradiction categories: '+','.join(contradicted)
                assert any(word in reply for word in ('发','转达','说过','让')),'Recipient failed to recall peer request'
                print('PASS: real sender routed colloquial request to correct peer, real recipient sent user 1, switch-off allowed explicit request, receipts and memory agree, retries did not duplicate')
        finally:store.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('FAIL:',type(exc).__name__)
        if isinstance(exc,AssertionError):print(str(exc))
        raise SystemExit(1)
