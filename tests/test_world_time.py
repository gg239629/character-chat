import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock,MagicMock
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import actors
import chat_history
import role_actions
import server
import storage
import world_time
from story_reset import reset_story


class WorldTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=storage.Store('sqlite:///'+(self.root/'test.db').as_posix());self.store.ensure('owner','default',{})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict('os.environ',{
            'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()})]
        for item in self.patches:item.start()
        reset_story('owner','default',self.root/'backups')

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def test_explicit_current_time_not_plans_questions_quotes_or_recollections(self):
        old={'day':1,'minute':480}
        def advance(text):return world_time.advance_candidate([{'role':'user','content':text}],old)
        self.assertEqual(advance('现在已经晚上十点了'),{'day':1,'minute':1320})
        self.assertEqual(advance('快进到第二天早晨八点半'),{'day':2,'minute':510})
        self.assertEqual(advance('现在下午3:30了'),{'day':1,'minute':930})
        self.assertEqual(advance('过了三个小时'),{'day':1,'minute':660})
        for text in ['明天晚上去吗？','如果现在到了晚上','你说现在晚上十点了','昨天现在是晚上','现在是不是晚上？','现在晚上要不要去公园？','我想快进到第二天，假如可以的话','现在早晨八点','只是又聊了几句话']:
            self.assertEqual(advance(text),old,text)
        self.assertEqual(world_time.advance_candidate([{'role':'assistant','content':'已经深夜了'}],old),old)

    def test_clock_advances_everyone_personally_without_creating_messages_or_leaking_private_content(self):
        def progress(data):world_time.apply(data,{'day':1,'minute':1200},{'wan'},time.time())
        self.store.mutate('owner','default',progress)
        statuses=[]
        for role in actors.ROLES:
            with role_actions.window('owner','default',role):
                _,payload=server.actor_payload(role,[{'role':'user','content':'你现在在干什么'}],False)
                info=next(message['content'] for message in payload if '全世界唯一的当前故事时间' in message['content'])
                self.assertIn('第1天 20:00',info)
                own=self.store.read('owner','default')['personal_progress'][role][-1]['status']
                self.assertIn(own,info)
                self.assertEqual(chat_history.messages(),[])
                self.assertTrue(all(other not in info for other in [key for key in actors.ROLES if key!=role]))
                self.assertTrue(len(own)<=10);statuses.append(own)
        self.assertGreater(len(set(statuses)),1)
        data=self.store.read('owner','default')
        before=json.dumps(data['personal_progress'],ensure_ascii=False)
        self.store.mutate('owner','default',lambda data:world_time.apply(data,{'day':1,'minute':1200},{'yan'},time.time()))
        self.assertEqual(json.dumps(self.store.read('owner','default')['personal_progress'],ensure_ascii=False),before)

    def test_private_stream_advances_before_answer_and_retry_does_not_advance_twice(self):
        seen=[]
        async def create(**kwargs):
            seen.append(json.dumps(kwargs['messages'],ensure_ascii=False))
            async def chunks():yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='小婉：晚上好。'))])
            return chunks()
        async def close():pass
        model=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
        cfg={'api_key':'fake','model':'test','base_url':'','temperature':.2}
        body={'request_id':'advance','messages':[{'role':'user','content':'快进到第二天晚上八点'}]}
        with patch.object(server,'load_config',return_value=cfg),patch.object(server,'make_client',return_value=model),TestClient(server.app) as client:
            headers={'X-Chat-Channel':'wan'}
            response=client.post('/api/stream',headers=headers,json=body)
            self.assertIn('"done": true',response.text)
            self.assertIn('第2天 20:00',seen[0])
            client.post('/api/stream',headers=headers,json=body)
            self.assertEqual(len(seen),1)
            for role in ['group',*actors.ROLES]:
                self.assertEqual(client.get('/api/info',headers={'X-Chat-Channel':role}).json()['scene_state']['时间'],'第2天 20:00')
            root=self.store.read('owner','default')
            for role in ['yan','liu']:
                self.assertEqual(len(root['personal_progress'][role]),1)
                with actors.role_scope('owner','default',role):self.assertEqual(chat_history.messages(),[])
            # Delayed maintenance for the same relative-time sentence cannot advance a third day.
            with role_actions.window('owner','default','wan'),patch('openai.OpenAI',side_effect=RuntimeError('skip')):
                revision=server.scene_revision.set(time.time()+1)
                try:server.update_scene_state(body['messages'],cfg_override=cfg)
                finally:server.scene_revision.reset(revision)
            self.assertEqual(self.store.read('owner','default')['story_clock']['day'],2)

    def test_old_scene_cannot_override_a_new_time_period(self):
        now=time.time()+1
        self.store.mutate('owner','default',lambda data:world_time.apply(data,{'day':1,'minute':1200},{'wan'},now))
        fake=MagicMock();fake.__enter__.return_value=fake
        fake.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'时间':'早上','小研':{'地点':'卧室','动作':'起床'}})))])
        with role_actions.window('owner','default','group'),patch('openai.OpenAI',return_value=fake):
            revision=server.scene_revision.set(now-1)
            try:server.update_scene_state([{'role':'user','content':'你好'}],cfg_override={'api_key':'fake','model':'test','base_url':''})
            finally:server.scene_revision.reset(revision)
            state=server.displayed_scene()
            self.assertEqual(state['时间'],'第1天 20:00')
            self.assertEqual(state['小研'],'书房·阅读')
