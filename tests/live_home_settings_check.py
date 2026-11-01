"""Real-model smoke test of normal replies with unsolicited work disabled."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import storage
import server
import role_actions


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'recent_log':[],'scene_state':{}})
        prompt=root/'prompt.md';prompt.write_text('日常角色聊天，只回复本人简短自然台词。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),\
                patch.object(server,'PROMPT_FILE',prompt),\
                patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{
                    'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                assert client.put('/api/home/settings',json={'initiative_enabled':False}).status_code==200
                with patch.object(role_actions,'ask_json',new=AsyncMock(side_effect=AssertionError('Unsolicited model call'))) as ask:
                    assert asyncio.run(role_actions.autonomous('owner','default','yan')) is None
                    ask.assert_not_awaited()
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={
                    'request_id':'normal-while-off','messages':[{'role':'user','content':'早上好，小研。简单回应我一句就好。'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert response.status_code==200 and any(e.get('done') for e in events),'Normal reply incomplete'
                assert not any(e.get('error') for e in events),'Normal model reply failed'
                messages=client.get('/api/messages',headers={'X-Chat-Channel':'yan'}).json()['messages']
                assert len(messages)==2 and messages[-1]['role']=='assistant','Switch blocked normal model reply'
                result=client.post('/api/home/reset',json={'confirm':True})
                assert result.status_code==200 and result.json()['backup'] is None,'Reset created backup or failed'
                for channel in ('group','yan','wan','liu'):
                    assert client.get('/api/messages',headers={'X-Chat-Channel':channel}).json()['messages']==[],'History survived reset'
                assert store.read('owner','default')['story_clock']=={'day':1,'minute':480}
                assert not client.get('/api/home/settings').json()['initiative_enabled'],'Reset changed switch'
                print('PASS: unsolicited calls blocked, actual character model still replies, no-backup reset clears all windows and starts first morning')
        finally:store.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('FAIL:',type(exc).__name__)
        if isinstance(exc,AssertionError):print(str(exc))
        raise SystemExit(1)
