import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
import storage
import server
import actors
import chat_history
import role_actions
import peer_chat
import initiative_control
import story_reset


class HomeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=storage.Store('sqlite:///'+(self.root/'chat.db').as_posix())
        self.store.ensure('owner','default',{})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict('os.environ',{
            'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),
            patch.object(server,'load_config',return_value={'api_key':'fake','model':'fake','temperature':.2,'base_url':''})]
        for p in self.patches:p.start()

    def tearDown(self):
        import memory_autosave
        memory_autosave.flush_due(force=True)
        for p in reversed(self.patches):p.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def test_switch_persists_and_stops_unsolicited_model_calls_and_queued_actions(self):
        with TestClient(server.app) as client:
            self.assertTrue(client.get('/api/home/settings').json()['initiative_enabled'])
            self.assertFalse(client.put('/api/home/settings',json={'initiative_enabled':False}).json()['initiative_enabled'])
            with role_actions.window('owner','default','yan'):
                chat_history.bootstrap();chat_history.append('old',0,'user','周末计划')
            action=role_actions.insert_action('owner','default','yan','group','autonomous','pending',{'decision':'send','message':'主动消息'})
            with patch.object(role_actions,'ask_json',new=AsyncMock()) as ask:
                self.assertIsNone(asyncio.run(role_actions.autonomous('owner','default','yan')))
                self.assertEqual(asyncio.run(role_actions.deliver(action['id']))['status'],'pending')
                self.assertIsNone(peer_chat.enqueue_inquiry('owner','default','yan','yan','unknown','她在哪','我不清楚'))
                ask.assert_not_awaited()
            self.assertFalse(initiative_control.enabled('owner','default'))
            self.assertTrue(client.put('/api/home/settings',json={'initiative_enabled':True}).json()['initiative_enabled'])
            self.assertEqual(asyncio.run(role_actions.deliver(action['id']))['status'],'sent')

    def test_user_requested_peer_question_still_gets_answer_while_proactive_is_off(self):
        with TestClient(server.app) as client:
            client.put('/api/home/settings',json={'initiative_enabled':False})
            auto=role_actions.insert_action('owner','default','yan','liu','autonomous','auto',{'decision':'send','message':'主动聊聊'})
            explicit=role_actions.insert_action('owner','default','yan','liu','requested','human',{'decision':'send','message':'用户让我问你面好了吗'})
            self.assertEqual(asyncio.run(role_actions.deliver(auto['id']))['status'],'pending')
            result=asyncio.run(role_actions.deliver(explicit['id']))
            self.assertEqual(result['status'],'sent')
            reply_id=peer_chat.key('reply',result['detail']['message_id'])
            with patch.object(role_actions,'ask_json',new=AsyncMock(return_value={'decision':'send','message':'我不知道。'})) as ask:
                asyncio.run(peer_chat.process(reply_id));ask.assert_awaited_once()
            self.assertEqual(len(peer_chat.history('owner','default','yan','liu')),2)

    def test_reset_deletes_all_memory_without_backup_and_keeps_configuration(self):
        for channel in ('group',*actors.ROLES):
            with role_actions.window('owner','default',channel):
                chat_history.bootstrap();chat_history.append('old',0,'user','旧聊天')
                storage.write_document('long_summaries',[{'text':'旧摘要'}])
                storage.write_document('relationship','旧亲密关系')
                storage.write_document('settings',{'model':'keep-model'})
        peer_chat.send('owner','default','yan','liu','旧私信','old-peer')
        initiative_control.set_enabled('owner','default',False)
        with TestClient(server.app) as client,patch.object(Path,'open',side_effect=AssertionError('Must not create a backup')):
            self.assertEqual(client.post('/api/home/reset',json={'confirm':False}).status_code,422)
            response=client.post('/api/home/reset',json={'confirm':True})
            self.assertEqual(response.status_code,200)
            self.assertIsNone(response.json()['backup'])
        root=self.store.read('owner','default')
        self.assertEqual(root['story_clock'],{'day':1,'minute':480})
        self.assertFalse(root['initiative_enabled'])
        self.assertEqual(peer_chat.history('owner','default','yan','liu'),[])
        with Session(self.store.engine) as db:self.assertEqual(list(db.scalars(select(peer_chat.Job))),[])
        for channel in ('group',*actors.ROLES):
            with role_actions.window('owner','default',channel):
                self.assertEqual(chat_history.messages(),[])
                self.assertEqual(storage.read_document('long_summaries',[]),[])
                self.assertEqual(storage.read_document('relationship',''),'')
                self.assertEqual(storage.read_document('settings',{})['model'],'keep-model')
        self.assertFalse((self.root/'backups').exists())

    def test_reset_busy_does_not_delete_messages(self):
        with TestClient(server.app) as client:
            chat_history.bootstrap();chat_history.append('old',0,'user','保留记录')
            chat_history.claim('active')
            try:
                self.assertEqual(client.post('/api/home/reset',json={'confirm':True}).status_code,409)
                self.assertEqual(chat_history.messages()[0]['content'],'保留记录')
            finally:chat_history.release('active')


if __name__=='__main__':unittest.main()
