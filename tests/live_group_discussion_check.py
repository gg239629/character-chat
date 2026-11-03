"""Actual coordinator and actor models, isolated group conversation and original history."""
import json
import re
import asyncio
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.runtime import role_actions
import server
from backend.app.infrastructure import storage


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Missing model configuration'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'recent_log':[],'scene_state':{'时间':'第1天 08:00','在场人物':'手机群聊'}})
        config=root/'model.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        prompt=root/'daily.md';prompt.write_text('普通朋友微信群聊天。三人独立，语言简短自然。对方角色问你可以直接答她，不必总是面向用户；无需强行延长聊天。',encoding='utf-8')
        actual_payload=server.actor_payload;calls=[]
        def payload(role,messages,group):
            calls.append((role,[dict(m) for m in messages]))
            return actual_payload(role,messages,group)
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',config),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()}),TestClient(server.app) as client:
                profiles={'wan':'直接爽快，喜欢植物园，周末有空。和小研小柳熟悉，别人问具体安排会直接答，有想法也会问她们。',
                          'yan':'认真细致，出游时会向提出方案的人确认出发时间和交通，直接和朋友讨论，不需要用户代为传话。',
                          'liu':'独立随和，喜欢花草，出游时愿意分享自己的建议，有不同意见会直接对朋友说。'}
                for role,persona in profiles.items():
                    response=client.post('/api/settings',headers={'X-Chat-Channel':role},json={'persona':persona,'public_persona':persona,'max_tokens':180})
                    assert response.status_code==200
                with role_actions.window('owner','default','group'):
                    chat_history.bootstrap()
                    chat_history.append('seed',0,'user','周末大家一起出去玩，先商量去哪儿。')
                    chat_history.append('seed',1,'assistant','小婉：我想去植物园，小研、小柳，你们觉得呢？','小婉')
                with patch.object(server,'actor_payload',side_effect=payload):
                    response=client.post('/api/stream',json={'request_id':'live-discussion','messages':[{'role':'user','content':'你们自己接着商量目的地和出发时间就好，直接和彼此说，不用每句都回答我。'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                errors=[re.findall(r'[A-Za-z]+(?:Error|Exception)',str(e['error'])) for e in events if e.get('error')]
                assert response.status_code==200 and any(e.get('done') for e in events),'Incomplete: HTTP '+str(response.status_code)+'; error types='+str(errors)+'; event keys='+str([list(e) for e in events])
                assert not any(e.get('error') for e in events),'Real group provider error types='+str(errors)
                generated=client.get('/api/messages').json()['messages'][3:]
                assert 2<=len(generated)<=8,'No natural group discussion'
                judgment=asyncio.run(role_actions.ask_json(cfg,
                    '你是严格的聊天测试审查员。判断后续回复是否有直接接前一角色的问题或建议，而非每人独立回答用户。称呼名字不是必要条件，例如前人问几点、后人回答九点也算。只输出JSON {"peer_interaction":true/false}。',
                    {'opening':'小婉：我想去植物园，小研、小柳，你们觉得呢？','actual_messages':[{'speaker':m['speaker'],'content':m['content']} for m in generated]}))
                assert judgment.get('peer_interaction') is True,'Actual replies did not engage with peers'
                assert len(set(m['id'] for m in generated))==len(generated)
                active=calls
                initial_size=len(active[0][1])
                for i,(role,history) in enumerate(active):
                    if len(history)>initial_size and generated:
                        assert any(m['role']=='assistant' and m['content']==generated[0]['content'] for m in history),'Later actor did not see first accepted reply'
                print('PASS actual models: peer-directed group discussion, sequential accepted context, natural completion; replies='+str(len(generated))+'; speakers='+','.join(m['speaker'] for m in generated))
        finally:store.engine.dispose()


if __name__=='__main__':main()
