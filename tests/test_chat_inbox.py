import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as S
from unittest.mock import patch,AsyncMock
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session
import storage
import server
import actors
import chat_history
import chat_inbox
import role_actions


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'test.db').as_posix())
        self.store.ensure('owner','default',{'recent_log':[],'scene_state':{}})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict('os.environ',{
            'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),
            patch.object(server,'load_config',return_value={'api_key':'fake','model':'test','base_url':'','temperature':.2})]
        for p in self.patches:p.start()

    def tearDown(self):
        import memory_autosave
        memory_autosave.flush_due(force=True)
        for p in reversed(self.patches):p.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def test_send_accepted_while_generation_lease_is_busy_and_idempotent(self):
        with TestClient(server.app) as client:
            chat_history.claim('already-generating')
            try:
                first=client.post('/api/send',json={'request_id':'first','messages':[{'role':'user','content':'先聊晚饭'}]})
                second=client.post('/api/send',json={'request_id':'second','messages':[{'role':'user','content':'等等，改聊周末'}]})
                self.assertEqual(first.status_code,200);self.assertEqual(second.status_code,200)
                again=client.post('/api/send',json={'request_id':'second','messages':[{'role':'user','content':'等等，改聊周末'}]})
                self.assertEqual(again.json()['id'],second.json()['id'])
                self.assertEqual([m['content'] for m in chat_history.messages()],['先聊晚饭','等等，改聊周末'])
                # Accepted original is already durable, independent of minute buffer.
                with Session(self.store.engine) as db:self.assertEqual(len(list(db.scalars(select(chat_history.Message)))),2)
            finally:chat_history.release('already-generating')

    def test_newest_turn_sees_all_messages_and_older_pending_is_superseded(self):
        captured=[]
        async def create(**kwargs):
            captured.append(kwargs['messages'])
            async def chunks():yield S(choices=[S(delta=S(content='小研：好，周末去植物园。'))])
            return chunks()
        model=S(chat=S(completions=S(create=create)),close=AsyncMock())
        with TestClient(server.app) as client:
            headers={'X-Chat-Channel':'yan'}
            first=client.post('/api/send',headers=headers,json={'request_id':'a','messages':[{'role':'user','content':'先聊晚饭'}]}).json()
            last=client.post('/api/send',headers=headers,json={'request_id':'b','messages':[{'role':'user','content':'等等，周末去植物园'}]}).json()
            with patch.object(server,'make_client',return_value=model):
                asyncio.run(chat_inbox.process(last['id']));asyncio.run(chat_inbox.process(first['id']))
            # An older job must not reopen the previous topic, even after newer completed.
            self.assertEqual(len(captured),1)
            text=json.dumps(captured,ensure_ascii=False)
            self.assertIn('先聊晚饭',text);self.assertIn('周末去植物园',text)
            self.assertEqual(len(client.get('/api/messages',headers=headers).json()['messages']),3)

    def test_mid_generation_interrupt_discards_unsent_answer_and_uses_new_context(self):
        async def scenario():
            with role_actions.window('owner','default','yan'):
                first=chat_inbox.accept(server.ChatIn(request_id='before',messages=[{'role':'user','content':'聊晚饭'}]))
            started=asyncio.Event();release=asyncio.Event();payloads=[]
            async def create(**kwargs):
                payloads.append(kwargs['messages'])
                async def chunks():
                    if len(payloads)==1:
                        started.set();await release.wait()
                    yield S(choices=[S(delta=S(content='小研：'+('旧晚饭答复' if len(payloads)==1 else '好，改聊周末。')))])
                return chunks()
            model=S(chat=S(completions=S(create=create)),close=AsyncMock())
            with patch.object(server,'make_client',return_value=model):
                task=asyncio.create_task(chat_inbox.process(first['id']))
                await asyncio.wait_for(started.wait(),5)
                with role_actions.window('owner','default','yan'):
                    second=chat_inbox.accept(server.ChatIn(request_id='interrupt',messages=[{'role':'user','content':'先别聊晚饭，改聊周末'}]))
                release.set();await task
                with role_actions.window('owner','default','yan'):
                    self.assertEqual([m['role'] for m in chat_history.messages()],['user','user'])
                await chat_inbox.process(second['id'])
            self.assertIn('先别聊晚饭',json.dumps(payloads[-1],ensure_ascii=False))
            with role_actions.window('owner','default','yan'):
                messages=chat_history.messages();self.assertEqual(len(messages),3)
                self.assertNotIn('旧晚饭答复',json.dumps(messages,ensure_ascii=False))
        with TestClient(server.app):asyncio.run(scenario())

    def test_status_is_scoped_to_original_window_and_content_cannot_change_on_retry(self):
        with TestClient(server.app) as client:
            body={'request_id':'scoped','messages':[{'role':'user','content':'你好'}]}
            item=client.post('/api/send',headers={'X-Chat-Channel':'yan'},json=body).json()
            self.assertEqual(client.get('/api/send/'+item['id'],headers={'X-Chat-Channel':'yan'}).status_code,200)
            self.assertEqual(client.get('/api/send/'+item['id'],headers={'X-Chat-Channel':'liu'}).status_code,404)
            self.assertEqual(client.get('/api/send/'+item['id']).status_code,404)
            body['messages'][0]['content']='不能替换原文'
            self.assertEqual(client.post('/api/send',headers={'X-Chat-Channel':'yan'},json=body).status_code,422)

    def test_worker_dispatches_latest_per_window_and_recovers_expired_jobs(self):
        with TestClient(server.app) as client:
            first=client.post('/api/send',json={'request_id':'older','messages':[{'role':'user','content':'第一句'}]}).json()
            last=client.post('/api/send',json={'request_id':'newer','messages':[{'role':'user','content':'第二句'}]}).json()
            private=client.post('/api/send',headers={'X-Chat-Channel':'yan'},json={'request_id':'private','messages':[{'role':'user','content':'私聊'}]}).json()
            with patch.object(chat_inbox,'process',new=AsyncMock()) as process:
                chat_inbox.run()
                ids={call.args[0] for call in process.await_args_list}
                self.assertEqual(ids,{last['id'],private['id']})
                self.assertNotIn(first['id'],ids)


if __name__=='__main__':unittest.main()
