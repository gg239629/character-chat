import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from sqlalchemy import select
from sqlalchemy.orm import Session
import actors
import chat_history
import group_coordinator
import long_memory
import role_actions
import role_memory
import server
import storage


class RoleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'test.db').as_posix())
        self.store.ensure('owner','default',{'scene_state':{'时间':'早晨'},'recent_log':[]})
        self.patch=patch.object(storage,'_STORE',self.store);self.patch.start()
        for role in actors.ROLES:
            with actors.role_scope('owner','default',role):
                chat_history.bootstrap();chat_history.append('private-'+role,0,'user','私密密码-'+role)

    def tearDown(self):
        self.patch.stop();self.store.engine.dispose();self.tmp.cleanup()

    def test_permissions_are_filtered_before_weighting_and_budget_is_bounded(self):
        chat_history.bootstrap()
        for i in range(30):chat_history.append('public-'+str(i),0,'assistant','公共讨论植物园兰花'+str(i),'小研')
        for group in [True,False]:
            memory=role_memory.remember('owner','default','wan','私密密码-yan',group)
            text=json.dumps(memory,ensure_ascii=False)
            self.assertNotIn('私密密码-yan',text);self.assertNotIn('私密密码-liu',text)
            self.assertIn('私密密码-wan',text)
            self.assertLessEqual(sum(memory['used'].values()),role_memory.BUDGET)
            self.assertTrue(all(event['channel'] in ['group','wan'] for event in memory['events']))
        public=role_memory.remember('owner','default','wan','植物园兰花',False)
        private=role_memory.remember('owner','default','wan','私密密码-wan',False)
        self.assertGreater(public['public_share'],private['public_share'])
        self.assertTrue(any(event['channel']=='group' for event in public['events']))

    def test_per_role_preferences_are_independent_and_archives_keep_provenance(self):
        for role,pref in [('wan',.8),('yan',.4)]:
            with actors.role_scope('owner','default',role):
                storage.write_document('memory_preferences',{'group_public':pref})
        a=role_memory.remember('owner','default','wan','无关话题',True)
        b=role_memory.remember('owner','default','yan','无关话题',True)
        self.assertGreater(a['public_share'],b['public_share'])
        with actors.role_scope('owner','default','wan'):
            storage.write_document('pending_archive',[{'id':str(i),'who':'他' if i%2==0 else '小婉','text':'约好去植物园看兰花','saved_at':'2026-11-01T08:00:00+08:00','action_id':'linked-event'} for i in range(50)])
            fake=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'summary':'私聊里约好去植物园看兰花，尚未出发。','facts':['约好去植物园看兰花。']},ensure_ascii=False)))])
            from unittest.mock import MagicMock
            model=MagicMock();model.__enter__.return_value=model;model.chat.completions.create.return_value=fake
            with patch('openai.OpenAI',return_value=model):self.assertTrue(long_memory.refresh_summary({'api_key':'fake','model':'test'}))
            summary=storage.read_document('long_summaries',[])[0]
            self.assertEqual(summary['channel'],'wan');self.assertEqual(summary['actor_id'],'wan')
            self.assertEqual(summary['visibility'],'private');self.assertEqual(summary['audience'],['user','wan'])
            self.assertEqual(summary['action_ids'],['linked-event']);self.assertEqual(len(summary['source_ids']),50)
            self.assertEqual(storage.read_document('important_facts',[])[0]['channel'],'wan')
        # Search long-term records even when newest direct messages are unrelated.
        memory=role_memory.remember('owner','default','wan','植物园兰花',False)
        self.assertTrue(any(event.get('kind')=='long_summaries' and event['channel']=='wan' for event in memory['events']))
        other=role_memory.remember('owner','default','yan','植物园兰花',False)
        self.assertNotIn('私聊里约好',json.dumps(other,ensure_ascii=False))

    def test_public_coordinator_uses_no_private_persona_or_state_and_validates_order(self):
        with actors.role_scope('owner','default','wan'):
            storage.write_document('public_persona','公开：热情')
            storage.write_document('persona','私人角色秘密')
            storage.write_document('relationship','私密关系')
        self.store.mutate('owner','default',lambda data:data.update(role_states={'wan':{'status':'私密位置'}}))
        async def decide(cfg,system,context):
            text=json.dumps(context,ensure_ascii=False)
            self.assertNotIn('私密密码',text);self.assertNotIn('私人角色秘密',text)
            self.assertNotIn('私密关系',text);self.assertNotIn('私密位置',text)
            self.assertIn('公开：热情',text)
            return {'order':['yan','wan']}
        with role_actions.window('owner','default','group'),patch.object(server,'load_config',return_value={'api_key':'fake','model':'test'}),patch.object(role_actions,'ask_json',side_effect=decide):
            self.assertEqual(asyncio.run(group_coordinator.plan([{'role':'user','content':'早上好'}],{'wan':({'api_key':'fake','model':'test'},[])})),['yan','wan'])
        with role_actions.window('owner','default','group'),patch.object(server,'load_config',return_value={'api_key':'fake','model':'test'}),patch.object(role_actions,'ask_json',new=AsyncMock(return_value={'order':['wan','wan','other']})):
            self.assertEqual(asyncio.run(group_coordinator.plan([{'role':'user','content':'早上好'}],{'wan':({'api_key':'fake','model':'test'},[])})),['wan','yan','liu'])

    def test_empty_coordinator_order_is_valid_and_does_not_force_a_speaker(self):
        with role_actions.window('owner','default','group'),patch.object(server,'load_config',return_value={'api_key':'fake','model':'test'}),patch.object(role_actions,'ask_json',new=AsyncMock(return_value={'order':[]})):
            self.assertEqual(asyncio.run(group_coordinator.plan([{'role':'user','content':'不用回复'}],{'wan':({'api_key':'fake','model':'test'},[])})),[])
