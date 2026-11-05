import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.runtime import role_actions as actions
import server
from backend.app.infrastructure import storage


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
            for i in range(8):
                plan['message']='小研：新的进展'+str(i)
                self.assertEqual(asyncio.run(actions.autonomous('owner','default','yan'))['status'],'sent')
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

    def test_repeated_initiative_is_cancelled_but_new_user_turn_can_be_answered(self):
        original='小研：（松了口气）起来了就好，我刚才都打算再催你一次了……那接下来这会儿，是归我的吧。'
        variant='小研：（揪着袖子晃了晃）起来了就好，我刚才都打算再催你一次了……那接下来这会儿，是归我的吧。'
        def send(source,message,kind='autonomous'):
            action=actions.insert_action('owner','default','yan','user',kind,source,{'decision':'send','message':message})
            return asyncio.run(actions.deliver(action['id']))
        self.assertEqual(send('original',original)['status'],'sent')
        self.assertEqual(send('duplicate',original)['status'],'cancelled')
        self.assertEqual(send('cosmetic',variant)['status'],'cancelled')
        with actions.window('owner','default','yan'):
            self.assertEqual(len(chat_history.messages()),2)
            chat_history.append('new-user',0,'user','你刚才说什么？再说一次')
        self.assertEqual(send('new-turn',original)['status'],'sent')
        # Explicit user-authorized deliveries retain their exact wording.
        self.assertEqual(send('requested',original,'requested')['status'],'sent')

    def test_initiative_dedup_does_not_suppress_new_content_or_other_channels(self):
        from backend.app.characters.reply_quality import repeated_initiative
        history=[{'role':'assistant','speaker':'小研','content':'小研：我已经到家了，今天路上看到一只小猫。'}]
        self.assertTrue(repeated_initiative(history[0]['content'],'小研',history))
        self.assertFalse(repeated_initiative(history[0]['content'],'小婉',history))
        self.assertFalse(repeated_initiative('小研：我给你留了一块蛋糕，记得回来尝尝。','小研',history))

    def test_autonomous_photo_creates_real_job_without_exposing_prompt(self):
        from backend.app.characters import image_generation as images,image_tools
        images.save_settings('owner',dict(enabled=True,inherit_connection=True,model='image',base_url='',api_key=''))
        plan={'decision':'send','target':'user','message':'小研：等我擦擦手，给你看看。','image_prompt':'厨房里本人入镜的自拍','next_check_seconds':60}
        with patch.object(actions,'ask_json',new=AsyncMock(return_value=plan)),patch.object(image_tools,'start'):
            result=asyncio.run(actions.autonomous('owner','default','yan'))
            self.assertEqual(result['status'],'sent')
            asyncio.run(actions.deliver(result['id']))
        with actions.window('owner','default','yan'):
            jobs=images.recent('owner','default','yan')
            self.assertEqual(len(jobs),1)
            self.assertEqual(jobs[0]['prompt'],'厨房里本人入镜的自拍')
            messages=chat_history.messages()
            self.assertEqual(len(messages),2)
            self.assertNotIn('厨房里本人入镜的自拍',messages[-1]['content'])

    def test_legacy_autonomous_photo_marker_is_converted_to_private_plan(self):
        action=actions.insert_action('owner','default','yan','user','autonomous','legacy-photo',
            {'decision':'send','message':'小研：行，就一张。\n[[action:image]]\n（厨房里的自拍）'})
        self.assertEqual(action['detail']['message'],'小研：行，就一张。')
        self.assertEqual(action['detail']['image_prompt'],'厨房里的自拍')

    def test_autonomous_message_queues_semantic_state_update(self):
        from backend.app.infrastructure import maintenance
        plan={'decision':'send','target':'user','message':'小研：（把茶杯放回桌上）刚泡的，尝尝？','next_check_seconds':60}
        with patch.object(actions,'ask_json',new=AsyncMock(return_value=plan)):
            result=asyncio.run(actions.autonomous('owner','default','yan'))
        self.assertEqual(result['status'],'sent')
        with Session(self.store.engine) as db:
            jobs=list(db.scalars(__import__('sqlalchemy').select(maintenance.Job).where(maintenance.Job.kind=='scene')))
        self.assertEqual(len(jobs),1);self.assertEqual(jobs[0].channel,'yan')
