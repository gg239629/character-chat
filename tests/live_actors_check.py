"""Opt-in real model end-to-end actor test; temporary DB and configuration only."""
import json
import time
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
import server
import storage
from role_routing import LABEL


def await_maintenance(store):
    import maintenance
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        with Session(store.engine) as db:
            states=list(db.scalars(select(maintenance.Job.status)))
        assert 'failed' not in states,'Background maintenance failed'
        if states and all(state=='done' for state in states):return
        time.sleep(.1)
    raise AssertionError('Background maintenance timeout')


def main():
    config=server.load_config()
    assert not server.check_cfg(config),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        store=storage.Store('sqlite:///'+(root/'chat.db').as_posix())
        store.ensure('owner','default',{'scene_state':{'时间':'周五下午','地点':'客厅','在场人物':'他、小婉、小研、小柳'},
            'recent_log':[{'who':'他','text':'我们约定周六一起去植物园看兰花。'}]})
        prompt=root/'prompt.md';prompt.write_text('这是日常角色聊天。小婉、小研、小柳以各自姓名标注台词，简短自然，每人不超过两句话。',encoding='utf-8')
        cfgfile=root/'model.json';cfgfile.write_text(json.dumps(config),encoding='utf-8')
        seen=[]
        original=server.make_client
        def track(cfg):
            seen.append(cfg.get('temperature'))
            return original(cfg)
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',cfgfile),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'missing'),patch.dict('os.environ',{'APP_ENCRYPTION_KEY':Fernet.generate_key().decode()}),TestClient(server.app) as client:
                for i,role in enumerate(server.PRIVATE_NAMES):
                    response=client.post('/api/settings',headers={'X-Chat-Channel':role},json={'model':config['model'],'base_url':config.get('base_url',''),'api_key':config['api_key'],'temperature':.21+i*.1,'max_tokens':350,'persona':'喜欢植物，回复简短，不代替其他人发言。'})
                    assert response.status_code==200
                with patch.object(server,'make_client',side_effect=track):
                    response=client.post('/api/stream',json={'request_id':'live-group','messages':[{'role':'user','content':'你们各自说一句，我们约好周六去哪里做什么？'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Group generation failed'
                done=next(e for e in events if e.get('done'))
                replies=[m for m in done['messages'] if m['role']=='assistant']
                assert replies and len(replies)<=3 and len({m['speaker'] for m in replies})==len(replies),'Invalid group participation'
                assert all(m['speaker'] in server.PRIVATE_NAMES.values() for m in replies),'Unknown speaker'
                assert all('植物园' in m['content'] or '兰花' in m['content'] for m in replies),'Public recall failed: '+json.dumps([m['content'] for m in replies],ensure_ascii=True)
                assert len(seen)>=2 and all(any(abs(value-expected)<.001 for expected in [.21,.31,.41]) for value in seen[1:]),'Actor configuration mismatch'
                assert done.get('maintenance_pending'),'Maintenance was not queued'
                await_maintenance(store)
                assert client.get('/api/memory/export').json()['scene_state'],'Scene not persisted'
                print('PASS: group considered three independent role configurations; participating actors recalled public fact; one scene persisted')
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={'request_id':'live-private','messages':[{'role':'user','content':'我们周六约好的安排是什么？请你自己回答。'}]})
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                assert not any(e.get('error') for e in events),'Private generation failed'
                messages=client.get('/api/messages',headers={'X-Chat-Channel':'yan'}).json()['messages']
                reply=messages[-1]
                assert reply['speaker']=='小研' and set(LABEL.findall(reply['content']))=={'小研'}
                assert '植物园' in reply['content'] or '兰花' in reply['content']
                assert len(client.get('/api/messages').json()['messages'])==len(done['messages']),'Private message leaked to group'
                await_maintenance(store)
                world_data=store.read('owner','default')
                assert world_data.get('role_states',{}).get('yan',{}).get('status'),'Private role state not persisted'
                activity=client.get('/api/conversations').json()[0]['activity']['yan']
                assert activity['unread']==1 and activity['preview']==reply['content'][:180]
                assert client.post('/api/messages/read',headers={'X-Chat-Channel':'yan'},json={'message_id':reply['id']}).status_code==200
                assert client.get('/api/conversations').json()[0]['activity']['yan']['unread']==0
                print('PASS: private reply used the same role, recalled public plan, and autosaved without leaking into group')
                print('PASS: private state persisted; last-message preview and read/unread cursor match actual messages')
                states=[client.get('/api/info',headers={'X-Chat-Channel':channel}).json()['scene_state'] for channel in ['group','wan','yan','liu']]
                assert all(state==states[0] for state in states),'World status diverged across windows'
                print('PASS: real private scene update is identical in all four windows')
                import maintenance
                storage.append_log([{'who':'他' if i%2==0 else '小研','text':str(i)+'：周六去植物园看兰花，还未出发；用户不吃花生。'} for i in range(250)])
                maintenance.enqueue('owner','default','group','real-archive-job',time.time(),scene=False)
                await_maintenance(store)
                summaries=store.read('owner','default').get('long_summaries',[])
                assert summaries and '植物园' in summaries[-1]['text'] and len(summaries[-1]['text'])<=260,'Background archive failed'
                assert store.read('owner','default').get('original_backups'),'Archived originals not preserved'
                print('PASS: real archival model ran in the background queue, saved a short summary and preserved originals')
        finally:
            store.engine.dispose()


if __name__=='__main__':
    main()
