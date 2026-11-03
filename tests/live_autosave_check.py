"""Real provider response followed by an actual background minute checkpoint."""
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
from backend.app.conversations import chat_history
from backend.app.infrastructure import maintenance
from backend.app.memory import memory_autosave
import server
from backend.app.infrastructure import storage
from backend.app.world.story_reset import reset_story


async def checkpoint():
    worker=maintenance.Worker();worker.start()
    try:
        for _ in range(65):
            if not memory_autosave.all_pending():return
            await asyncio.sleep(1)
        raise AssertionError('Minute checkpoint did not complete')
    finally:await worker.stop()


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix());store.ensure('owner','default',{})
        config=root/'model.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        prompt=root/'daily.md';prompt.write_text('普通日常聊天，简短回答。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',config),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()}),TestClient(server.app) as client:
                reset_story('owner','default',root/'backups')
                response=client.post('/api/stream',headers={'X-Chat-Channel':'wan'},json={'request_id':'minute-live','messages':[{'role':'user','content':'早上好，今天想喝茶还是咖啡？'}]})
                assert response.status_code==200
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Provider error'
                assert any(e.get('done') for e in events)
                before=client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()
                assert len(before['messages'])>=2 and before['save_pending']
                with Session(store.engine) as db:assert not list(db.scalars(select(chat_history.Message)))
                print('PASS real reply readable before database checkpoint',flush=True)
                asyncio.run(checkpoint())
                with Session(store.engine) as db:assert len(list(db.scalars(select(chat_history.Message))))>=2
                after=client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()
                assert not after['save_pending'] and before['messages']==after['messages']
                print('PASS actual minute worker persisted originals and memory without duplicates',flush=True)
        finally:store.engine.dispose()


if __name__=='__main__':main()
