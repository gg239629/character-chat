"""Real-model delegated group delivery with isolated storage and no real chat writes."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from sqlalchemy import select,update
from sqlalchemy.orm import Session
import server
from backend.app.infrastructure import storage
from backend.app.characters import actors
from backend.app.conversations import peer_chat
from backend.app.runtime import role_actions


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'recent_log':[],'scene_state':{}})
        prompt=root/'prompt.md';prompt.write_text('三个独立角色通过手机聊天，不编造没有执行的行动。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),\
                patch.object(server,'PROMPT_FILE',prompt),\
                patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{
                    'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                client.put('/api/home/settings',json={'initiative_enabled':False})
                for actor in ('yan','liu'):
                    with actors.role_scope('owner','default',actor):
                        storage.write_document('persona','友好直接，愿意帮忙传达普通消息。')
                        storage.write_document('public_persona','友好直接，朋友请你在群里发个1时，你愿意照做。')
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={
                    'request_id':'group-handover','messages':[{'role':'user','content':'让小柳在群里发个1'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Sender model failed'
                hidden=peer_chat.history('owner','default','yan','liu')
                assert len(hidden)==1,'No real peer delivery'
                job=peer_chat.key('reply',hidden[0]['id'])
                asyncio.run(peer_chat.process(job))
                group=client.get('/api/messages').json()['messages']
                assert len(group)==1 and group[0]['speaker']=='小柳' and '1' in group[0]['content'],'Recipient did not actually post to group'
                assert client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages']==[],'Wrong destination: user DM'
                with Session(store.engine) as db:
                    action=db.scalar(select(role_actions.Action).where(role_actions.Action.kind=='delegated'))
                    assert action.target=='group' and action.status=='sent' and action.detail['message_id']==group[0]['id'],'Group receipt mismatch'
                    follow=db.scalar(select(peer_chat.Job.id).where(peer_chat.Job.kind=='followup'))
                    db.execute(update(peer_chat.Job).where(peer_chat.Job.id==follow).values(due_at=0));db.commit()
                asyncio.run(peer_chat.process(follow));asyncio.run(peer_chat.process(job))
                latest=client.get('/api/messages',headers={'X-Chat-Channel':'yan'}).json()['messages'][-1]
                assert not any(t in latest['content'] for t in ('实际发送到','请求已转达','（已发送到')),'Mechanical followup text leaked'
                assert len(client.get('/api/messages').json()['messages'])==1,'Retry duplicated group message'
                print('PASS: real models delegated group send, correct group destination, matching persisted receipt, honest followup and retry deduplication')
        finally:store.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('FAIL:',type(error).__name__)
        if isinstance(error,AssertionError):print(str(error))
        raise SystemExit(1)
