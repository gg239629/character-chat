"""Real recall, concise archival input and provider usage in temporary storage."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.memory import long_memory
from backend.app.infrastructure import model_usage
from backend.app.runtime import role_actions
from backend.app.memory import role_memory
import server
from backend.app.infrastructure import storage


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Missing model configuration'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix());store.ensure('owner','default',{'recent_log':[]})
        config=root/'model.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        prompt=root/'daily.md';prompt.write_text('普通朋友日常聊天，简短自然，记不清时不要编造。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',config),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()}),TestClient(server.app) as client:
                with role_actions.window('owner','default','group'):
                    chat_history.bootstrap();chat_history.append('plan',0,'user','我们约定周日一起去植物园看兰花。')
                    for i in range(15):chat_history.append('daily-'+str(i),0,'assistant','小柳：今天浇过花了。','小柳')
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={'request_id':'recall-efficient','messages':[{'role':'user','content':'还记得我们约定周日去哪里、看什么吗？'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert response.status_code==200 and any(e.get('done') for e in events) and not any(e.get('error') for e in events),'Real reply failed'
                reply=client.get('/api/messages',headers={'X-Chat-Channel':'yan'}).json()['messages'][-1]['content']
                assert '植物园' in reply and '兰花' in reply,'Cross-window recall regressed'
                with role_actions.window('owner','default','group'):
                    memory=role_memory.remember('owner','default','yan','植物园兰花',False,budget=8000)
                    raw=len(json.dumps(memory['events'],ensure_ascii=False,separators=(',',':')))
                    compact=len(json.dumps([role_memory.compact(e) for e in memory['events']],ensure_ascii=False,separators=(',',':')))
                    assert compact<raw
                    records=[{'id':str(i),'who':'他' if i%2==0 else '小研','text':'原来约定植物园周六去，现在明确改到周日去，还没有出发。' if i==0 else '大家讨论植物园的兰花，日常聊天。','saved_at':'2026-11-01T08:00:00+08:00'} for i in range(50)]
                    storage.write_document('pending_archive',records)
                    assert long_memory.refresh_summary(cfg),'Real archive failed'
                    assert len(storage.read_document('original_backups',[]))==50
                    assert len(storage.read_document('long_summaries',[])[-1]['text'])<=260
                usage=client.get('/api/usage').json()['categories']
                assert usage['character']['calls']>=1 and usage['archive']['calls']==1
                print('PASS real private reply recalls public plan; archival summary bounded, originals preserved; metadata characters '+str(raw)+' -> '+str(compact),flush=True)
                print('PASS provider usage: character input tokens='+str(usage['character']['input_tokens'])+'; output tokens='+str(usage['character']['output_tokens'])+'; calls with usage='+str(usage['character']['calls_with_token_usage']),flush=True)
        finally:store.engine.dispose()


if __name__=='__main__':main()
