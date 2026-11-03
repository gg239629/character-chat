import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.runtime import role_actions as actions
import server
from backend.app.infrastructure import storage


class RoleActionTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import AsyncMock
        from backend.app.characters import actors
        self.coordinator_patch=patch.object(server.group_coordinator,'plan',new=AsyncMock(side_effect=lambda messages,configs:[role for role in actors.candidates(messages) if not any(m.get('role')=='assistant' and m.get('content','').startswith(actors.ROLES[role]+'：') for m in server.recent_rounds(messages,1))]))
        self.coordinator_patch.start()
        self.addCleanup(self.coordinator_patch.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'chat.db').as_posix())
        self.store.ensure('owner','default',{'scene_state':{},'recent_log':[]})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict('os.environ',{
            'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),
            patch.object(server,'load_config',return_value={'api_key':'fake','model':'fake','base_url':'','temperature':.2}),
            patch.object(server,'update_scene_state',return_value={}),patch.object(server.long_memory,'refresh_summary')]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def test_cross_send_is_real_audited_and_not_duplicated(self):
        decision={'decision':'send','message':'小研：大家周末想去哪儿？','reply':'好，我来问问。','reason':'愿意帮忙'}
        body={'request_id':'cross-once','messages':[{'role':'user','content':'你在群里问问大家周末想去哪儿'}]}
        with TestClient(server.app) as client,patch.object(actions,'ask_json',new=AsyncMock(return_value=decision)):
            response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json=body)
            self.assertIn('"done": true',response.text)
            group=client.get('/api/messages').json()['messages']
            self.assertEqual([m['content'] for m in group],['小研：大家周末想去哪儿？'])
            private=client.get('/api/messages',headers={'X-Chat-Channel':'yan'}).json()['messages']
            self.assertNotIn('已发送到群聊',private[-1]['content']);self.assertIn('好，我来问问',private[-1]['content'])
            audit=client.get('/api/actions',headers={'X-Chat-Channel':'yan'}).json()['actions']
            self.assertEqual(audit[0]['status'],'sent')
            self.assertEqual(audit[0]['detail']['message_id'],group[0]['id'])
            client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json=body)
            self.assertEqual(len(client.get('/api/messages').json()['messages']),1)
            self.assertEqual(client.get('/api/conversations').json()[0]['activity']['group']['unread'],1)

    def test_delivered_message_survives_physical_metadata_failure(self):
        action=actions.insert_action('owner','default','yan','group','requested','metadata-failure',{
            'decision':'send','message':'小研：大家来吃饭。'})
        with patch('backend.app.world.world_events.capture',side_effect=RuntimeError('metadata unavailable')):
            result=asyncio.run(actions.deliver(action['id']))
        self.assertEqual(result['status'],'sent')
        with actions.window('owner','default','group'):
            self.assertEqual([m['content'] for m in chat_history.messages()],['小研：大家来吃饭。'])
        with Session(self.store.engine) as db:
            self.assertEqual(db.get(actions.Action,action['id']).status,'sent')

    def test_refusal_and_clarification_never_send(self):
        with TestClient(server.app) as client:
            for i,choice in enumerate(('refuse','ask')):
                decision={'decision':choice,'reply':'这件事我不想替你说。' if choice=='refuse' else '你想让我发什么？','reason':'尊重性格'}
                with patch.object(actions,'ask_json',new=AsyncMock(return_value=decision)):
                    response=client.post('/api/stream',headers={'X-Chat-Channel':'wan'},json={'request_id':'refuse-'+str(i),'messages':[{'role':'user','content':'去群里说句话'}]})
                self.assertIn('"done": true',response.text)
            self.assertEqual(client.get('/api/messages').json()['messages'],[])
            self.assertEqual({a['status'] for a in client.get('/api/actions').json()['actions']},{'refuse','ask'})

    def test_delayed_send_cancel_and_owner_validation(self):
        action=actions.insert_action('owner','default','yan','group','requested','later',{
            'decision':'later','message':'小研：稍后见','delay_minutes':5,'reason':'正在忙'})
        self.assertEqual(asyncio.run(actions.deliver(action['id']))['status'],'pending')
        with TestClient(server.app) as client:
            self.assertEqual(client.post('/api/actions/'+action['id']+'/cancel',headers={'X-Chat-Channel':'wan'}).status_code,404)
            self.assertEqual(client.post('/api/actions/'+action['id']+'/cancel',headers={'X-Chat-Channel':'yan'}).status_code,200)
        self.assertEqual(asyncio.run(actions.deliver(action['id']))['status'],'cancelled')
        self.assertFalse(actions.cancel('other','default',action['id']))

    def test_scheduled_action_delivered_once_after_restart(self):
        action=actions.insert_action('owner','default','liu','group','requested','due',{
            'decision':'later','message':'小柳：约好的提醒','delay_minutes':1},now=time.time()-120)
        asyncio.run(actions.deliver(action['id']))
        asyncio.run(actions.deliver(action['id']))
        with actions.window('owner','default','group'):
            self.assertEqual([m['content'] for m in chat_history.messages()],['小柳：约好的提醒'])

    def test_target_leases_and_failures_do_not_claim_delivery(self):
        action=actions.insert_action('owner','default','yan','group','requested','busy',{'decision':'send','message':'小研：稍等'})
        with actions.window('owner','default','group'):
            self.assertTrue(chat_history.claim('busy-target'))
            result=asyncio.run(actions.deliver(action['id']))
            self.assertEqual(result['status'],'pending')
            self.assertEqual(chat_history.messages(),[])
            chat_history.release('busy-target')

    def prepare_old_private(self):
        with actions.window('owner','default','yan'):
            storage.write_document('behavior',{'enabled':True,'quiet_start':0,'quiet_end':0})
            chat_history.bootstrap()
            chat_history.append('old',0,'user','昨天约好周末去植物园。')
            from backend.app.memory import memory_autosave
            memory_autosave.flush('owner',actors.role_id('default','yan'),force=True)
            with Session(self.store.engine) as db:
                db.execute(update(chat_history.Message).values(created_at=time.time()-7200));db.commit()

    def test_proactive_limits_quiet_and_no_send_cooldown(self):
        self.prepare_old_private()
        answer=AsyncMock(return_value={'decision':'none','reason':'目前没有必要打扰'})
        with patch.object(actions,'ask_json',new=answer):
            self.assertEqual(asyncio.run(actions.autonomous('owner','default','yan'))['status'],'none')
            self.assertIsNone(asyncio.run(actions.autonomous('owner','default','yan')))
            self.assertEqual(answer.await_count,1)
        policy=actions.Behavior(enabled=True,quiet_start=22,quiet_end=8)
        midnight=datetime(2026,11,1,1,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.assertTrue(actions.eligible('owner','default','yan',policy,midnight))
        self.assertTrue(actions.eligible('owner','default','yan',actions.Behavior(),time.time()))

    def test_proactive_private_send_no_repeated_unanswered_contact(self):
        self.prepare_old_private()
        now=time.time()
        answer=AsyncMock(return_value={'decision':'send','message':'小研：周末的植物园计划，几点出发？','reason':'未完成约定'})
        with patch.object(actions,'ask_json',new=answer):
            result=asyncio.run(actions.autonomous('owner','default','yan',now))
            self.assertEqual(result['status'],'sent')
            self.assertEqual(asyncio.run(actions.autonomous('owner','default','yan',now+4*3600))['status'],'pending')
            self.assertEqual(answer.await_count,2)
        with actions.window('owner','default','yan'):
            self.assertIn('植物园',chat_history.messages()[-1]['content'])

    def test_public_autonomy_excludes_all_private_context(self):
        self.prepare_old_private()
        with actors.role_scope('owner','default','yan'):
            storage.write_document('relationship','private-relationship-secret')
            storage.write_document('behavior',{'enabled':True,'allow_private':False,'allow_group':True,'quiet_start':0,'quiet_end':0})
        captured=[]
        async def decide(cfg,system,context):
            captured.append(context)
            return {'decision':'send','target':'group','message':'小研：大家今天有空吗？','reason':'公开话题'}
        with patch.object(actions,'ask_json',new=decide):
            self.assertEqual(asyncio.run(actions.autonomous('owner','default','yan'))['status'],'sent')
        self.assertNotIn('private_history',captured[-1])
        self.assertNotIn('private-relationship-secret',json.dumps(captured[-1],ensure_ascii=False))
        self.assertNotIn('昨天约好周末去植物园',json.dumps(captured[-1],ensure_ascii=False))

    def test_peer_delivery_is_hidden_from_all_user_windows(self):
        action=actions.insert_action('owner','default','yan','liu','requested','peer',{'decision':'send','message':'小研：周末有空吗？'})
        self.assertEqual(asyncio.run(actions.deliver(action['id']))['status'],'sent')
        with TestClient(server.app) as client:
            self.assertEqual(client.get('/api/messages').json()['messages'],[])
            self.assertEqual(client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages'],[])
            messages=client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages']
            self.assertEqual(messages,[])
            self.assertEqual(client.get('/api/actions').json()['actions'],[])
            self.assertNotIn('周末有空吗',client.get('/api/memory/export',headers={'X-Chat-Channel':'liu'}).text)
        from backend.app.conversations import peer_chat
        self.assertEqual(peer_chat.history('owner','default','liu','yan')[-1]['sender'],'yan')

    def test_cron_requires_token_and_behavior_timezone_validation(self):
        with TestClient(server.app) as client,patch.dict('os.environ',{'ROLE_CRON_TOKEN':'x'*40}),patch.object(actions,'tick',new=AsyncMock()) as tick:
            self.assertEqual(client.post('/api/jobs/tick').status_code,401)
            self.assertEqual(client.post('/api/jobs/tick',headers={'Authorization':'Bearer '+'x'*40}).status_code,200)
            tick.assert_awaited_once()
            self.assertEqual(client.post('/api/settings',headers={'X-Chat-Channel':'yan'},json={'behavior':{'timezone':'not-a-timezone'}}).status_code,422)

    def test_bounded_peer_reply_does_not_leak_user_private_history(self):
        with actors.role_scope('owner','default','liu'):
            storage.write_document('behavior',{'quiet_start':0,'quiet_end':0,'respond_to_roles':True})
            chat_history.bootstrap();chat_history.append('secret',0,'user','private-liu-user-secret')
        action=actions.insert_action('owner','default','yan','liu','requested','peer-reaction',{'decision':'send','message':'小研：今天一起看书吗？'})
        asyncio.run(actions.deliver(action['id']))
        captured=[]
        async def decide(cfg,system,context):
            captured.append(context)
            return {'decision':'send','message':'小柳：好，什么时候？','reason':'愿意交流'}
        with patch.object(actions,'ask_json',new=decide):
            from backend.app.conversations import peer_chat
            with Session(self.store.engine) as db:
                job=db.scalar(select(peer_chat.Job.id).where(peer_chat.Job.kind=='reply'))
            asyncio.run(peer_chat.process(job))
            asyncio.run(peer_chat.process(job))
            self.assertEqual(len(captured),1)
        self.assertNotIn('private-liu-user-secret',json.dumps(captured))
        replies=peer_chat.history('owner','default','yan','liu')
        self.assertEqual(len(replies),2)
        self.assertEqual(replies[-1]['sender'],'liu')
        with actions.window('owner','default','yan'):
            self.assertEqual(chat_history.messages(),[])


    def test_group_requests_private_delivery_even_when_coordinator_nominates_nobody(self):
        contexts=[]
        async def decide(cfg,system,context):
            contexts.append(context)
            self.assertEqual(context['origin'],'group')
            self.assertEqual(context['target'],'你与用户的私聊')
            return {'decision':'send','message':'1','reply':'好。','reason':'愿意'}
        body={'request_id':'group-private-all','messages':[{'role':'user','content':'你们都私信给我发个1'}]}
        with TestClient(server.app) as client,patch.object(server.group_coordinator,'plan',new=AsyncMock(return_value=[])),patch.object(actions,'ask_json',side_effect=decide):
            response=client.post('/api/stream',json=body)
            self.assertIn('"done": true',response.text)
            for role in actors.ROLES:
                messages=client.get('/api/messages',headers={'X-Chat-Channel':role}).json()['messages']
                self.assertEqual(len(messages),1)
                self.assertEqual(messages[0]['content'],actors.ROLES[role]+'：1')
                self.assertEqual(messages[0]['speaker'],actors.ROLES[role])
                audit=client.get('/api/actions',headers={'X-Chat-Channel':role}).json()['actions']
                self.assertEqual(audit[0]['status'],'sent');self.assertEqual(audit[0]['target'],'user')
                self.assertEqual(audit[0]['detail']['audience'],['user',role])
            activity=client.get('/api/conversations').json()[0]['activity']
            self.assertTrue(all(activity[role]['unread']==1 for role in actors.ROLES))
            self.assertEqual(len(contexts),3)
            self.assertEqual(len(storage.read_document('recent_log',[])[0]['action_ids']),3)
            client.post('/api/stream',json=body)
            self.assertEqual(len(contexts),3)
            self.assertTrue(all(len(client.get('/api/messages',headers={'X-Chat-Channel':role}).json()['messages'])==1 for role in actors.ROLES))

    def test_named_group_private_request_does_not_send_for_other_roles(self):
        decision={'decision':'send','message':'1','reply':'好。'}
        async def create(**kwargs):
            async def chunks():yield type('Chunk',(),{'choices':[type('Choice',(),{'delta':type('Delta',(),{'content':actors.SILENCE})()})()]})()
            return chunks()
        from types import SimpleNamespace
        async def close():pass
        model=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
        with TestClient(server.app) as client,patch.object(actions,'ask_json',new=AsyncMock(return_value=decision)) as decide,patch.object(server,'make_client',return_value=model):
            response=client.post('/api/stream',json={'request_id':'group-private-one','messages':[{'role':'user','content':'小研，你私信给我发个1'}]})
            self.assertIn('"done": true',response.text)
            self.assertEqual(decide.await_count,1)
            self.assertEqual(len(client.get('/api/messages',headers={'X-Chat-Channel':'yan'}).json()['messages']),1)
            for role in ['wan','liu']:self.assertEqual(client.get('/api/messages',headers={'X-Chat-Channel':role}).json()['messages'],[])

    def test_group_refusal_or_busy_target_never_claims_successful_private_delivery(self):
        with TestClient(server.app) as client,patch.object(server.group_coordinator,'plan',new=AsyncMock(return_value=[])):
            for choice in ['refuse','ask']:
                with patch.object(actions,'ask_json',new=AsyncMock(return_value={'decision':choice,'reply':'我先不发。'})):
                    response=client.post('/api/stream',json={'request_id':'group-'+choice,'messages':[{'role':'user','content':'小婉私信我发1'}]})
                    self.assertNotIn('已发送到与你的私聊',response.text)
                self.assertEqual(client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages'],[])
            with actions.window('owner','default','wan'):
                chat_history.claim('private-busy')
            with patch.object(actions,'ask_json',new=AsyncMock(return_value={'decision':'send','message':'1','reply':'好。'})):
                response=client.post('/api/stream',json={'request_id':'group-queued','messages':[{'role':'user','content':'小婉私信给我发1'}]})
                self.assertNotIn('已发送到与你的私聊',response.text)
                action=client.get('/api/actions').json()['actions'][0]
                self.assertEqual(action['status'],'pending')
            with actions.window('owner','default','wan'):chat_history.release('private-busy')
            with Session(self.store.engine) as db:
                db.execute(update(actions.Action).where(actions.Action.id==action['id']).values(due_at=0));db.commit()
            asyncio.run(actions.deliver(action['id']))
            self.assertEqual(len(client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages']),1)


if __name__=='__main__':
    unittest.main()
