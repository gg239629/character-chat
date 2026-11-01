import asyncio
import json
import unittest
from unittest.mock import patch
from sqlalchemy import select,inspect
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import storage
import server
import accounts
import chat_history
import chat_inbox
import message_service
import event_memory
import task_health


class CoreTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_failed_task_commit_rolls_back_original_and_memory(self):
        with TestClient(server.app) as client:
            original=Session.commit
            def fail(db):
                if any(isinstance(row,chat_inbox.Turn) for row in db.new):raise RuntimeError('simulated task commit failure')
                return original(db)
            with patch.object(Session,'commit',fail):
                with self.assertRaises(Exception):
                    client.post('/api/send',json={'request_id':'atomic','messages':[{'role':'user','content':'must rollback'}]})
            self.assertEqual(chat_history.messages(),[])
            self.assertNotIn('must rollback',json.dumps(storage.current_data()))

    def test_observed_actions_do_not_leak_and_hypotheses_do_not_become_events(self):
        event_memory.capture('owner','default','yan',{'id':'real','role':'assistant','speaker':'小研','created_at':1,'content':'小研：（取出礼物，放下盒子）'})
        event_memory.capture('owner','default','yan',{'id':'plan','role':'assistant','speaker':'小研','created_at':2,'content':'小研：（准备出门买礼物）'})
        own=event_memory.recall('owner','default','yan','礼物')
        self.assertEqual(len(own),1)
        self.assertEqual(own[0]['source_id'],'real')
        self.assertEqual(event_memory.recall('owner','default','liu','礼物'),[])

    def test_append_does_not_cancel_draft_but_explicit_topic_change_does(self):
        self.assertFalse(message_service.cancels_draft([{'role':'user','content':'还有小柳呢','created_at':2}],1))
        self.assertTrue(message_service.cancels_draft([{'role':'user','content':'先别聊这个，改聊周末','created_at':2}],1))

    def test_schema_versions_idempotent_and_history_preserved(self):
        import schema_migrations
        schema_migrations.upgrade(self.store.engine);schema_migrations.upgrade(self.store.engine)
        self.assertIn('ix_message_channel_clock',{i['name'] for i in inspect(self.store.engine).get_indexes('chat_messages')})
        self.assertEqual(self.store.read('owner','default'),{})

    def test_diagnostics_store_only_error_category(self):
        task_health.record('owner','default','task','reply',ValueError('secret-key-and-private-content'))
        report=task_health.report('owner','default')
        self.assertEqual(report[0]['error_type'],'ValueError')
        self.assertNotIn('secret',str(report))

    def test_account_users_are_isolated_and_logout_revokes_session(self):
        with patch.dict('os.environ',{'ENABLE_USER_ACCOUNTS':'true'}),TestClient(server.app) as client:
            self.assertEqual(client.get('/api/messages').status_code,401)
            accounts.register('alice','test-password-a');accounts.register('bob','test-password-b')
            with patch.object(server,'auth_rate_limit'):
                self.assertEqual(client.post('/api/auth/login',json={'username':'alice','password':'wrong'}).status_code,401)
                response=client.post('/api/auth/login',json={'username':'alice','password':'test-password-a'})
                self.assertEqual(response.status_code,200)
                self.assertIn('HttpOnly',response.headers['set-cookie'])
            stories=client.get('/api/conversations').json()
            self.assertEqual(len(stories),1)
            alice_story=stories[0]['id']
            self.assertEqual(client.post('/api/send',json={'request_id':'alice-msg','messages':[{'role':'user','content':'Alice private'}]}).status_code,200)
            with patch.object(server,'auth_rate_limit'):
                client.post('/api/auth/login',json={'username':'bob','password':'test-password-b'})
            self.assertEqual(client.get('/api/messages').json()['messages'],[])
            self.assertEqual(client.get('/api/messages',headers={'X-Conversation-ID':alice_story}).status_code,404)
            self.assertEqual(client.get('/api/messages',headers={'X-User-ID':'owner'}).json()['messages'],[])
            token=client.cookies.get('chat_session')
            client.post('/api/auth/logout')
            self.assertIsNone(accounts.identify(token))
            with Session(self.store.engine) as db:
                self.assertNotIn('test-password',db.get(accounts.Account,'alice').password_hash)

    def test_failed_task_retry_keeps_original_and_is_scoped(self):
        with TestClient(server.app) as client:
            item=client.post('/api/send',json={'request_id':'retry','messages':[{'role':'user','content':'原消息'}]}).json()
            with Session(self.store.engine) as db:
                row=db.get(chat_inbox.Turn,item['id']);row.status='failed';row.attempts=3;db.commit()
            self.assertEqual(client.post('/api/send/'+item['id']+'/retry',headers={'X-Chat-Channel':'yan'}).status_code,404)
            self.assertEqual(client.post('/api/send/'+item['id']+'/retry').status_code,200)
            self.assertEqual(len(chat_history.messages()),1)
            self.assertEqual(client.post('/api/send/'+item['id']+'/retry').status_code,409)

    def test_unchanged_poll_omits_body_but_reset_is_visible(self):
        with TestClient(server.app) as client:
            client.post('/api/send',json={'request_id':'poll','messages':[{'role':'user','content':'原消息'}]})
            last=client.get('/api/messages').json()['messages'][-1]['id']
            response=client.get('/api/messages',params={'after':last}).json()
            self.assertTrue(response['unchanged']);self.assertEqual(response['messages'],[])
            client.post('/api/messages/clear-display')
            response=client.get('/api/messages',params={'after':last}).json()
            self.assertFalse(response['unchanged']);self.assertEqual(response['messages'],[])


if __name__=='__main__':unittest.main()
