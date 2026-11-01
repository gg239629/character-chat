import asyncio
import unittest
from unittest.mock import patch,AsyncMock
from sqlalchemy import select,event,text,inspect,create_engine
from sqlalchemy.orm import Session
import test_home_settings as fixtures
import storage
import chat_history
import chat_inbox
import peer_chat
import role_actions
import message_service
import server
import schema_migrations
from fastapi.testclient import TestClient


class ReliabilityTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_message_and_action_receipt_rollback_together_for_all_destinations(self):
        original=Session.commit
        for target in ('group','user','liu'):
            action=role_actions.insert_action('owner','default','yan',target,'requested','atomic-'+target,
                {'decision':'send','message':'atomic message'})
            def fail(db):
                if any(isinstance(r,role_actions.Action) and r.status=='sent' for r in db.dirty):
                    raise OSError('simulated commit failure')
                return original(db)
            with patch.object(Session,'commit',fail):
                result=asyncio.run(role_actions.deliver(action['id']))
            self.assertEqual(result['status'],'failed')
            with Session(self.store.engine) as db:
                self.assertEqual(list(db.scalars(select(chat_history.Message))),[])
                self.assertEqual(list(db.scalars(select(peer_chat.Message))),[])
                self.assertEqual(list(db.scalars(select(peer_chat.Job))),[])
                self.assertNotEqual(db.get(role_actions.Action,action['id']).status,'sent')

    def test_unchanged_poll_queries_only_indexed_head_and_handles_buffer_and_clear(self):
        import memory_autosave
        with TestClient(server.app):
            chat_history.bootstrap();chat_history.append('first',0,'user','original body')
            memory_autosave.flush(*storage.scope.get(),force=True)
            last=chat_history.messages()[-1]['id'];queries=[]
            def query(conn,cursor,statement,*args):queries.append(statement.lower())
            event.listen(self.store.engine,'before_cursor_execute',query)
            try:self.assertEqual(chat_history.poll_visible(last),([],True))
            finally:event.remove(self.store.engine,'before_cursor_execute',query)
            self.assertFalse(any('chat_messages.content' in q for q in queries))
            chat_history.append('second',0,'assistant','new buffered reply','小研')
            messages,unchanged=chat_history.poll_visible(last)
            self.assertFalse(unchanged);self.assertEqual(len(messages),2)
            chat_history.clear_display()
            self.assertEqual(chat_history.poll_visible(last),([],False))

    def test_busy_generation_does_not_exhaust_retry_budget(self):
        with TestClient(server.app) as client:
            task=client.post('/api/send',json={'request_id':'busy','messages':[{'role':'user','content':'hello'}]}).json()
            chat_history.claim('other-generator')
            try:asyncio.run(chat_inbox.process(task['id']))
            finally:chat_history.release('other-generator')
            with Session(self.store.engine) as db:
                row=db.get(chat_inbox.Turn,task['id'])
                self.assertEqual(row.status,'pending');self.assertEqual(row.attempts,0)
                self.assertGreater(row.next_attempt_at,0)

    def test_old_worker_cannot_overwrite_new_lease(self):
        async def scenario():
            with role_actions.window('owner','default','group'):
                task=chat_inbox.accept(server.ChatIn(request_id='lease',messages=[{'role':'user','content':'hello'}]))
            async def response(body):
                with Session(self.store.engine) as db:
                    row=db.get(chat_inbox.Turn,task['id']);row.token='new-worker';db.commit()
                return type('Result',(),{'status_code':409})()
            with patch.object(server,'chat_stream',side_effect=response):await chat_inbox.process(task['id'])
            with Session(self.store.engine) as db:
                row=db.get(chat_inbox.Turn,task['id']);self.assertEqual(row.status,'running');self.assertEqual(row.token,'new-worker')
        with TestClient(server.app):asyncio.run(scenario())

    def test_migration_adds_retry_fields_to_existing_database_preserving_rows(self):
        engine=create_engine('sqlite:///'+(self.root/'legacy.db').as_posix())
        try:
            with engine.begin() as db:
                db.execute(text('CREATE TABLE chat_inbox_turns (id VARCHAR PRIMARY KEY,status VARCHAR,created_at FLOAT)'))
                db.execute(text("INSERT INTO chat_inbox_turns VALUES ('old','pending',1)"))
            schema_migrations.upgrade(engine);schema_migrations.upgrade(engine)
            self.assertTrue({'token','next_attempt_at'}<={c['name'] for c in inspect(engine).get_columns('chat_inbox_turns')})
            with engine.connect() as db:
                row=db.execute(text("SELECT id, token,next_attempt_at FROM chat_inbox_turns")).one()
                self.assertEqual(tuple(row),('old','',0))
        finally:engine.dispose()
