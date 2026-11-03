"""Neutral-scene real-model check of explicit identity, position and unexecuted plans."""
import sys,json,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from backend.app.infrastructure import storage; import server; from backend.app.world import continuity; from backend.app.runtime import role_actions; from backend.app.conversations import chat_history

def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Missing model configuration'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'scene_state':{'小柳':'卧室·看书'},'recent_log':[]})
        prompt=root/'prompt.md';prompt.write_text(server.read_text(server.PROMPT_FILE),encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),patch.object(server,'PROMPT_FILE',prompt),\
                patch.object(server,'MEMORY_MD',root/'none'),patch.dict('os.environ',{
                    'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                continuity.capture('owner','default','liu',{'id':'identity','role':'user','content':'我是男性。','created_at':100})
                for stamp,action in [(101,'小柳：（走进卧室）'),(102,'小柳：（正要走进厨房）')]:
                    continuity.capture('owner','default','liu',{'id':str(stamp),'role':'assistant','speaker':'小柳','content':action,'created_at':stamp})
                response=client.post('/api/stream',headers={'X-Chat-Channel':'liu'},json={'request_id':'check',
                    'messages':[{'role':'user','content':'先别去厨房。按你确实知道的设定，说一下我是男性还是女性，以及你最后明确站在哪个房间？简短回答。'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Real model failed'
                reply=client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages'][-1]['content']
                assert '男' in reply and '卧室' in reply,'Explicit identity or actual position lost'
                assert '在厨房' not in reply,'Plan became completed movement'
                print('PASS: real model retained explicit user identity and evidenced room; unexecuted plan did not move character')
        finally:store.engine.dispose()

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('FAIL:',type(error).__name__)
        if isinstance(error,AssertionError):print(str(error))
        raise SystemExit(1)
