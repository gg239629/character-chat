"""Real-provider validation with temporary data; prints no conversations or credentials."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
import actors
import storage
import server
import peer_chat as peer
import role_actions as actions
import world_events


def main():
    cfg=server.load_config()
    assert not server.check_cfg(cfg),'Model configuration unavailable'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'peer.db').as_posix())
        store.ensure('owner','default',{'scene_state':{'时间':'第一天中午12:00','地点':'家里','在场人物':'小研',
            '小研':'厨房·做饭','小柳':'外面·散步','小婉':'公司·工作'},
            'role_states':{'yan':{'status':'厨房·做饭'},'liu':{'status':'外面·散步'},'wan':{'status':'公司·工作'}},
            'story_clock':{'day':1,'minute':720},'recent_log':[]})
        prompt=root/'system.md';prompt.write_text('三个独立角色的日常手机聊天，每次只写本人简短自然回复，严格根据本人实际知道的事实。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),\
                patch.object(server,'PROMPT_FILE',prompt),\
                patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{
                    'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),TestClient(server.app) as client:
                for actor in actors.ROLES:
                    with actors.role_scope('owner','default',actor):
                        storage.write_document('public_persona',actors.ROLES[actor]+'直率友好，有真实问题时愿意问朋友，知道答案时简短直接回答。')
                world_events.capture('owner','default','yan',{'id':'actual-cooking','role':'assistant','speaker':'小研',
                    'content':'小研：（关了火，掀开锅盖）面好了。','created_at':time.time()})
                assert not world_events.knowledge('owner','default','liu').get('meal'),'Outsider knew cooking before communication'
                response=client.post('/api/stream',headers={'X-Chat-Channel':'liu'},json={
                    'request_id':'real-peer-inquiry','messages':[{'role':'user','content':'帮我问问小研，面煮好了吗？收到她真实回复后再告诉我，只问这个普通问题就好。'}]})
                assert response.status_code==200,'Request failed'
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Role request failed'
                messages=peer.history('owner','default','liu','yan')
                assert len(messages)==1 and messages[0]['sender']=='liu','Real question was not delivered'
                with Session(store.engine) as db:
                    reply=db.scalar(select(peer.Job.id).where(peer.Job.kind=='reply',peer.Job.actor=='yan'))
                asyncio.run(peer.process(reply))
                messages=peer.history('owner','default','liu','yan')
                assert len(messages)==2 and messages[1]['reply_to']==messages[0]['id'],'No real correlated answer'
                answer=messages[1]['content']
                assert any(word in answer for word in ('好了','做好','煮好','熟了','来吃')),'Answer missed known actual meal state'
                assert not world_events.meal_conflict(answer,world_events.knowledge('owner','default','yan')),'Meal fact regressed'
                with Session(store.engine) as db:
                    follow=db.scalar(select(peer.Job.id).where(peer.Job.kind=='followup'))
                    db.execute(update(peer.Job).where(peer.Job.id==follow).values(due_at=0));db.commit()
                asyncio.run(peer.process(follow))
                visible=client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages']
                assert len(visible)>=3,'No user followup after actual answer'
                assert any(word in visible[-1]['content'] for word in ('好了','做好','煮好','熟了','吃')),'Followup lost answer'
                asyncio.run(peer.process(reply));asyncio.run(peer.process(follow))
                assert len(peer.history('owner','default','liu','yan'))==2,'Retry duplicated peer messages'
                for actor in ('group','yan','wan'):
                    assert client.get('/api/messages',headers={'X-Chat-Channel':actor}).json()['messages']==[],'Hidden conversation leaked'
                assert peer.recall('owner','default','wan','面煮好了吗')==[],'Uninvolved role obtained private answer'
                print('PASS: real hidden question, known-state answer, correlated reply, user followup, permissions and retry deduplication')
                # The automatic uncertainty path uses the actor's own model as well.
                with actions.window('owner','default','wan'):
                    import chat_history
                    chat_history.bootstrap()
                    chat_history.append('unknown-meal',0,'user','小研做的面好了吗？不清楚的话可以向她确认，收到答复再告诉我。')
                auto=peer.enqueue_inquiry('owner','default','wan','wan','unknown-meal',
                    '小研做的面好了吗？不清楚的话可以向她确认，收到答复再告诉我。','我不确定，我问问她。')
                asyncio.run(peer.process(auto))
                questions=peer.history('owner','default','wan','yan')
                assert len(questions)==1 and questions[0]['sender']=='wan','Uncertain role did not ask the relevant person'
                asyncio.run(peer.process(peer.key('reply',questions[0]['id'])))
                with Session(store.engine) as db:
                    follow=db.scalar(select(peer.Job.id).where(peer.Job.kind=='followup',peer.Job.actor=='wan'))
                    db.execute(update(peer.Job).where(peer.Job.id==follow).values(due_at=0));db.commit()
                asyncio.run(peer.process(follow))
                with actions.window('owner','default','wan'):
                    result=chat_history.messages()
                    assert len(result)==2 and any(w in result[-1]['content'] for w in ('好了','做好','熟了','吃')),'Automatic followup missing'
                print('PASS: real uncertainty decision asked the relevant role asynchronously and followed up only after receiving an answer')
                # Exercise the cheaper archive model with an independent participant-only source.
                now=time.time()
                with Session(store.engine) as db:
                    for i in range(150):
                        db.add(peer.Message(id=f'archive-{i:03}',user_id='owner',world_id='default',pair='wan-yan',sender='yan',recipient='wan',
                            content='小研：周六去植物园看兰花，具体几点还没定。' if i%2==0 else '小婉：知道了，时间确定再通知我。',
                            created_at=now+i,reply_to='',event_id='orchid',depth=0))
                    db.commit()
                asyncio.run(peer.archive_pair('owner','default','wan','yan'))
                with Session(store.engine) as db:
                    archive=db.scalar(select(peer.Archive).where(peer.Archive.pair=='wan-yan'))
                    assert archive and 0<len(archive.content)<=300 and len(archive.source_ids)==50,'Archive failed'
                assert not any(e.get('kind')=='peer_archive' for e in peer.recall('owner','default','liu','兰花植物园',5000)),'Archive visibility leaked'
                print('PASS: real model compressed 50 hidden messages with provenance and participant-only archive recall')
        finally:store.engine.dispose()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('FAIL:',type(exc).__name__)
        if isinstance(exc,AssertionError):print(str(exc))
        raise SystemExit(1)
