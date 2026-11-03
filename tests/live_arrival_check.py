"""Actual-model arrival regression, using disposable storage."""
import sys,json,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from backend.app.infrastructure import storage; import server; from backend.app.runtime import role_actions; from backend.app.conversations import chat_history


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Missing model configuration'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'recent_log':[],'scene_state':{'小婉':'路上·开车','小研':'厨房·做饭'}})
        prompt=root/'prompt.md';prompt.write_text('普通日常手机聊天，最新已发生的行动与用户明确纠正优先。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),\
                patch.object(server,'PROMPT_FILE',prompt),\
                patch.object(server,'MEMORY_MD',root/'missing'),patch.dict('os.environ',{'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),
                    'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                with role_actions.window('owner','default','wan'):
                    chat_history.bootstrap();chat_history.append('old',0,'user','坐车回家')
                    chat_history.append('old',1,'assistant','小婉：（握着方向盘开车）还在路上，等会儿。','小婉')
                response=client.post('/api/stream',headers={'X-Chat-Channel':'wan'},json={'request_id':'home-now',
                    'messages':[{'role':'user','content':'我们现在到家啦，已经停好车进屋了。'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Actual character model failed'
                messages=client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages']
                reply=messages[-1]['content']
                assert not any(s in reply for s in ('还没到','还在路上','我在开车','你先上楼')),'Model regressed to old driving state'
                scene=client.get('/api/scene',headers={'X-Chat-Channel':'wan'}).json()['scene_state']
                assert '开车' not in scene['小婉'],'Status remained driving'
                assert scene['小研']=='厨房·做饭','Private arrival moved unrelated role'
                with role_actions.window('owner','default','wan'):
                    updated=server.update_scene_state([{'role':'user','content':'我们现在到家啦，已经停好车进屋了。'},
                        {'role':'assistant','content':reply}],cfg_override=cfg)
                    assert updated and '开车' not in updated['小婉'],'Scene model reintroduced old driving state'
                print('PASS: real reply and scene model honor arrival before generation, no driving regression, unrelated role unchanged')
        finally:store.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('FAIL:',type(error).__name__)
        if isinstance(error,AssertionError):print(str(error))
        raise SystemExit(1)
