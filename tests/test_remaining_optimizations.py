import unittest
import json
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import storage
import chat_history
import memory_autosave
import message_service
import event_memory
import peer_chat
import task_health


class RemainingTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_buffered_and_durable_messages_have_same_provenance(self):
        with TestClient(__import__('server').app):
            with __import__('role_actions').window('owner','default','yan'):
                chat_history.bootstrap();chat_history.append('buffered',0,'assistant','小研：你好','小研')
                memory_autosave.flush(*storage.scope.get(),force=True)
                message_service.durable_append('durable',0,'assistant','小研：你好','小研')
                records=storage.get_store().read(*storage.scope.get())['recent_log']
                left={k:v for k,v in records[-2].items() if k not in ('id','saved_at')}
                right={k:v for k,v in records[-1].items() if k not in ('id','saved_at')}
                self.assertEqual(left,right)

    def test_delta_contains_only_new_records_and_reset_invalidates_cursor(self):
        with TestClient(__import__('server').app) as client:
            chat_history.bootstrap();chat_history.append('one',0,'user','第一句')
            memory_autosave.flush(*storage.scope.get(),force=True)
            first=chat_history.messages()[-1]['id']
            chat_history.append('two',0,'assistant','小研：第二句','小研')
            delta=client.get('/api/messages',params={'after':first,'incremental':True}).json()
            self.assertTrue(delta['delta']);self.assertEqual([m['content'] for m in delta['messages']],['小研：第二句'])
            second=delta['messages'][0]['id']
            # A cursor still in RAM remains valid before the minute checkpoint.
            self.assertTrue(client.get('/api/messages',params={'after':second,'incremental':True}).json()['unchanged'])
            client.post('/api/messages/clear-display')
            cleared=client.get('/api/messages',params={'after':second,'incremental':True}).json()
            self.assertFalse(cleared['delta']);self.assertEqual(cleared['messages'],[])

    def test_foreign_cursor_cannot_retrieve_other_private_messages(self):
        import role_actions
        with TestClient(__import__('server').app) as client:
            with role_actions.window('owner','default','yan'):
                chat_history.append('secret',0,'user','yan-only')
                foreign=chat_history.messages()[-1]['id']
            response=client.get('/api/messages',params={'after':foreign,'incremental':True},headers={'X-Chat-Channel':'liu'}).json()
            self.assertFalse(response['delta']);self.assertEqual(response['messages'],[])

    def test_observation_and_received_report_are_distinct(self):
        record={'id':'public-action','role':'assistant','speaker':'小研','content':'小研：（拿起快递，放下盒子）','created_at':1}
        event_memory.capture('owner','default','group',record)
        own=event_memory.recall('owner','default','yan','快递')
        peer=event_memory.recall('owner','default','liu','快递')
        self.assertEqual(own[0]['kind'],'observed_action')
        self.assertEqual(peer[0]['kind'],'reported_action')
        self.assertIn('不能当作本人目击',peer[0]['basis'])

    def test_private_report_known_only_by_actual_recipient(self):
        peer_chat.send('owner','default','yan','liu','小研：（取出礼物，放下礼盒）','gift')
        self.assertEqual(event_memory.recall('owner','default','liu','礼物')[0]['kind'],'reported_action')
        self.assertEqual(event_memory.recall('owner','default','wan','礼物'),[])
        self.assertEqual(len(self.store.read('owner','default')['witnessed_events']),1)

    def test_explicit_physical_handover_is_witnessed_but_same_home_is_not(self):
        event_memory.capture('owner','default','yan',{'id':'gift','role':'assistant','speaker':'小研',
            'content':'小研：（取出礼物，递给小柳）','created_at':1})
        self.assertEqual(event_memory.recall('owner','default','liu','礼物')[0]['kind'],'observed_action')
        self.assertEqual(event_memory.recall('owner','default','wan','礼物'),[])

    def test_diagnostics_retain_attempt_and_duration_without_error_text(self):
        task_health.record('owner','default','task','scene',RuntimeError('secret-provider-url'),2,1790)
        report=task_health.report('owner','default')[0]
        self.assertEqual(report['attempts'],2);self.assertEqual(report['elapsed_ms'],1790)
        self.assertNotIn('secret',json.dumps(report))

    def test_reported_lie_does_not_replace_actual_observation(self):
        event_memory.capture('owner','default','yan',{'id':'actual','role':'assistant','speaker':'小研',
            'content':'小研：（拿起快递，放下盒子）','created_at':1})
        peer_chat.send('owner','default','yan','liu','快递还没拿到。','claim')
        liu=event_memory.recall('owner','default','liu','快递')
        self.assertEqual(liu[0]['kind'],'reported_action')
        self.assertIn('还没拿到',liu[0]['text'])
        original=self.store.read('owner','default')['witnessed_events'][0]
        self.assertEqual(original['witnesses'],['yan']);self.assertEqual(original['source_id'],'actual')
        self.assertEqual(event_memory.recall('owner','default','wan','快递'),[])

    def test_lightweight_info_avoids_prompt_and_memory_building(self):
        import server
        with (TestClient(server.app) as client,
            patch.object(server,'build_system_prompt',side_effect=AssertionError('expensive prompt build')),
            patch.object(server,'build_fewshot',side_effect=AssertionError('expensive history build'))):
            self.assertEqual(client.get('/api/info',params={'details':False}).status_code,200)

    def test_shared_policy_rejects_nonparticipants_and_hidden_user_window(self):
        import channel_policy
        policy=channel_policy.resolve('owner','default','peer:yan-liu','yan')
        self.assertFalse(policy.user_visible);self.assertEqual(set(policy.participants),{'yan','liu'})
        with self.assertRaises(PermissionError):channel_policy.resolve('owner','default','peer:yan-liu','wan')
        with self.assertRaises(PermissionError):message_service.conversation('owner','default','peer:yan-liu')
        with self.assertRaises(PermissionError):channel_policy.resolve('another-user','default','group','yan')

    def test_diagnostics_migration_preserves_old_errors(self):
        from sqlalchemy import create_engine,text
        import schema_migrations
        engine=create_engine('sqlite:///'+(self.root/'old-health.db').as_posix())
        try:
            with engine.begin() as db:
                db.execute(text('CREATE TABLE chat_task_failures(id VARCHAR PRIMARY KEY,stage VARCHAR)'))
                db.execute(text("INSERT INTO chat_task_failures VALUES ('old','reply')"))
            schema_migrations.upgrade(engine);schema_migrations.upgrade(engine)
            with engine.connect() as db:
                self.assertEqual(tuple(db.execute(text('SELECT id,attempts,elapsed_ms FROM chat_task_failures')).one()),('old',0,0))
        finally:engine.dispose()

    def test_background_retry_requires_failed_task_and_same_story_owner(self):
        import maintenance
        import time
        from sqlalchemy import update
        maintenance.enqueue('owner','default','group','retry-scene',time.time())
        with Session(self.store.engine) as db:
            task=db.scalar(select(maintenance.Job).where(maintenance.Job.kind=='scene'))
            identity=task.id;task.status='failed';task.attempts=3;db.commit()
        self.store.ensure('owner','different',{})
        with self.assertRaises(PermissionError):task_health.retry('owner','different','scene',identity)
        self.assertEqual(task_health.retry('owner','default','scene',identity)['status'],'pending')
        with self.assertRaises(ValueError):task_health.retry('owner','default','scene',identity)
        with Session(self.store.engine) as db:self.assertEqual(db.get(maintenance.Job,identity).attempts,0)
