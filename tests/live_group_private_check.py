"""Real models: public requests deliver actual per-role private messages."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import actors
import server
import storage


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Missing model configuration'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix());store.ensure('owner','default',{'recent_log':[]})
        config=root/'model.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        prompt=root/'daily.md';prompt.write_text('普通微信角色聊天，用本人简短台词，不代替别人说话。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',config),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                for role in actors.ROLES:
                    response=client.post('/api/settings',headers={'X-Chat-Channel':role},json={'persona':'日常性格友善，愿意帮忙；明确、无隐私且很简单的发数字请求乐意执行，不替别人决定。','public_persona':'愿意回应明确的简单请求','max_tokens':200})
                    assert response.status_code==200
                body={'request_id':'real-group-dm','messages':[{'role':'user','content':'你们三个人现在分别私信给我发一个数字1，只需要1，每个人用自己的私聊窗口发给我。'}]}
                response=client.post('/api/stream',json=body)
                assert response.status_code==200
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(event.get('error') for event in events),'Real group request failed'
                assert any(event.get('done') for event in events)
                for role in actors.ROLES:
                    messages=client.get('/api/messages',headers={'X-Chat-Channel':role}).json()['messages']
                    assert len(messages)==1 and messages[0]['speaker']==actors.ROLES[role] and '1' in messages[0]['content'],'No private delivery for '+role
                    actions=client.get('/api/actions',headers={'X-Chat-Channel':role}).json()['actions']
                    assert actions[0]['status']=='sent' and actions[0]['target']=='user'
                activity=client.get('/api/conversations').json()[0]['activity']
                assert all(activity[role]['unread']==1 for role in actors.ROLES)
                response=client.post('/api/stream',json=body)
                assert all(len(client.get('/api/messages',headers={'X-Chat-Channel':role}).json()['messages'])==1 for role in actors.ROLES)
                print('PASS real group coordinator and three role models: each privately delivered 1, private unread previews updated, repeated request did not duplicate')
        finally:store.engine.dispose()


if __name__=='__main__':main()
