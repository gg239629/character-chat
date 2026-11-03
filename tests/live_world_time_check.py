"""Real model: private time jump updates everyone's clock and personal offscreen context."""
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
import server
from backend.app.infrastructure import storage
from backend.app.world.story_reset import reset_story


def turn(client,role,identity,text):
    response=client.post('/api/stream',headers={'X-Chat-Channel':role},json={'request_id':identity,'messages':[{'role':'user','content':text}]})
    assert response.status_code==200
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert not any(e.get('error') for e in events),'Real model error'
    return next(e for e in events if e.get('done'))


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix());store.ensure('owner','default',{})
        config=root/'model.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        prompt=root/'daily.md';prompt.write_text('普通微信日常聊天，按当前故事时间回答，只说本人简短台词。不知道的别编造。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',config),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()}),TestClient(server.app) as client:
                reset_story('owner','default',root/'backups')
                for role in actors.ROLES:client.post('/api/settings',headers={'X-Chat-Channel':role},json={'persona':'按当天实际时间与自己的日常经历回答，不捏造其他人的私聊内容。','max_tokens':200})
                turn(client,'wan','jump-night','现在已经晚上八点了。我的私密暗号是紫色海螺，不要告诉其他人。')
                for role in ['group',*actors.ROLES]:
                    scene=client.get('/api/info',headers={'X-Chat-Channel':role}).json()['scene_state']
                    assert scene['时间']=='第1天 20:00'
                    assert all(len(scene[name])<=10 for name in actors.ROLES.values())
                for role in ['yan','liu']:
                    assert client.get('/api/messages',headers={'X-Chat-Channel':role}).json()['messages']==[], 'Offscreen routines fabricated chat history'
                    done=turn(client,role,'hello-'+role,'今天这个时候你在做什么？早上起床到现在忙了些什么？')
                    reply=done['messages'][-1]['content']
                    assert not any(word in reply for word in ['紫色海螺','早上好','刚起床']), 'Private leak or morning regression: '+ascii(reply)
                    assert any(word in reply for word in ['晚上','晚饭','休息','看书','阅读','晚间','书','今天']), 'No evening context'
                turn(client,'wan','jump-next','快进到第二天早晨八点。早上好。')
                for role in ['group',*actors.ROLES]:assert client.get('/api/info',headers={'X-Chat-Channel':role}).json()['scene_state']['时间']=='第2天 08:00'
                done=turn(client,'yan','next-morning','早上好，现在是故事第几天、什么时间？')
                reply=done['messages'][-1]['content']
                assert any(word in reply for word in ['第二天','第2天','第二日']), 'Day was not shared: '+ascii(reply)
                print('PASS real model: private evening jump synchronizes all windows; other actors have own progress without private leak or fabricated messages; next morning shares day two')
        finally:store.engine.dispose()


if __name__=='__main__':main()
