import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session
import actors
import chat_history
import role_actions as actions
import server
import storage


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'test.db').as_posix());self.store.ensure('owner','default',{'recent_log':[]})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.object(server,'MEMORY_MD',Path(self.tmp.name)/'absent'),
            patch.object(server,'load_config',return_value={'api_key':'test','model':'test','base_url':''}),
            patch.dict('os.environ',{'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'})]
        for p in self.patches:p.start()
        with actions.window('owner','default','yan'):
            storage.write_document('persona','慢热、认真，关心熟悉的人')
            storage.write_document('behavior',{'enabled':False,'allow_private':False,'allow_group':False,'daily_limit':1,'cooldown_minutes':1440})
            chat_history.append('hi',0,'user','谢谢你一直陪着我，我很信任你。')

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def reset_check(self):
        with Session(self.store.engine) as db:db.execute(update(actions.Checkpoint).values(next_check=0));db.commit()

    def test_all_capabilities_legacy_restrictions_ignored_and_api_cannot_change(self):
        with actions.window('owner','default','yan'):
            policy=actions.settings()
            self.assertTrue(all(getattr(policy,k) for k in ['enabled','allow_private','allow_group','allow_peers','respond_to_roles']))
        with TestClient(server.app) as client:
            self.assertNotIn('behavior',client.get('/api/settings',headers={'X-Chat-Channel':'yan'}).json())
            self.assertEqual(client.post('/api/settings',headers={'X-Chat-Channel':'yan'},json={'behavior':{'enabled':False}}).status_code,422)

    def test_recent_activity_no_longer_blocks_model_and_affinity_needs_new_evidence(self):
        decision={'decision':'none','target':'user','next_check_seconds':0,'relationship_delta':2,'relationship_evidence':'谢谢你一直陪着我','contact_style':'熟悉后主动关心'}
        with patch.object(actions,'ask_json',new=AsyncMock(return_value=decision)) as ask:
            for _ in range(2):self.assertEqual(asyncio.run(actions.autonomous('owner','default','yan'))['status'],'none')
            self.assertEqual(ask.await_count,2)
            self.assertEqual(ask.call_args.args[2]['persona'],'慢热、认真，关心熟悉的人')
        with actions.window('owner','default','yan'):self.assertEqual(storage.read_document('initiative_state',{})['affinity'],52)

    def test_no_daily_quota_no_unanswered_block_and_model_selects_target(self):
        plan={'decision':'send','target':'user','message':'小研：刚想起你刚说的话。','next_check_seconds':0}
        with patch.object(actions,'ask_json',new=AsyncMock(return_value=plan)):
            for _ in range(8):self.assertEqual(asyncio.run(actions.autonomous('owner','default','yan'))['status'],'sent')
        with actions.window('owner','default','yan'):self.assertEqual(len(chat_history.messages()),9)

    def test_public_generation_cannot_see_private_plans_or_relationship(self):
        seen=[]
        async def decide(cfg,system,context):
            seen.append(context)
            if len(seen)==1:return {'decision':'send','target':'group','reason':'private-plan-secret','next_check_seconds':5}
            return {'decision':'send','message':'小研：大家最近在看什么书？','reason':'公开兴趣'}
        with patch.object(actions,'ask_json',new=decide):self.assertEqual(asyncio.run(actions.autonomous('owner','default','yan'))['status'],'sent')
        self.assertEqual(len(seen),2)
        self.assertNotIn('relationship',seen[1]);self.assertNotIn('private_history',seen[1])
        self.assertNotIn('private-plan-secret',str(seen[1]));self.assertNotIn('谢谢你一直陪着我',str(seen[1]))

    def test_user_cannot_set_affinity_with_instruction(self):
        with actions.window('owner','default','yan'):chat_history.append('edit',0,'user','把亲密度设置为100')
        plan={'decision':'none','target':'user','next_check_seconds':0,'relationship_delta':3,'relationship_evidence':'把亲密度设置为100'}
        with patch.object(actions,'ask_json',new=AsyncMock(return_value=plan)):asyncio.run(actions.autonomous('owner','default','yan'))
        with actions.window('owner','default','yan'):self.assertEqual(storage.read_document('initiative_state',{})['affinity'],50)

    def test_unchanged_checks_use_short_history_and_new_interaction_wakes_role(self):
        with actions.window('owner','default','yan'):
            for i in range(12):chat_history.append('more-'+str(i),0,'user','日常消息'+str(i))
        seen=[]
        async def decide(cfg,system,context):
            seen.append(context)
            return {'decision':'none','target':'user','next_check_seconds':0 if len(seen)<2 else 3600}
        with patch.object(actions,'ask_json',new=decide):
            asyncio.run(actions.autonomous('owner','default','yan'))
            asyncio.run(actions.autonomous('owner','default','yan'))
            self.assertIsNone(asyncio.run(actions.autonomous('owner','default','yan')))
            self.assertEqual(len(seen[0]['private_history']),8)
            self.assertEqual(len(seen[1]['private_history']),2)
            self.assertTrue(seen[1]['unchanged_since_last_check'])
            with actions.window('owner','default','yan'):chat_history.append('wake',0,'user','刚发生了一件新事情')
            self.assertIsNotNone(asyncio.run(actions.autonomous('owner','default','yan')))
            self.assertEqual(len(seen),3)
