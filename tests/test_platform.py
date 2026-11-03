import asyncio
import hashlib
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import text
import test_home_settings as fixtures
import server
from backend.app.infrastructure import storage
from backend.app.mcp import access
from backend.app.mcp import server as connector
from backend.app.characters.service import characters
from backend.app.conversations import chat_inbox
from backend.app.memory import retrieval


class PlatformTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_alembic_adopts_existing_schema_and_preserves_data(self):
        from backend.app.infrastructure.migrate import upgrade
        self.store.mutate('owner','default',lambda data:data.update(marker='keep'))
        upgrade(self.store.engine)
        self.assertEqual(self.store.read('owner','default')['marker'],'keep')
        with self.store.engine.connect() as conn:self.assertEqual(conn.execute(text('SELECT version_num FROM alembic_version')).scalar(),'0005_images')

    def test_mcp_credentials_are_scoped_revocable_and_digest_only(self):
        created=access.create('owner','test',['read','chat','wan'],daily_limit=1)
        actor=access.identify(created['token']);self.assertIsNotNone(actor)
        token=access.principal.set(actor)
        try:
            self.assertEqual([c['id'] for c in connector.list_characters()],['wan'])
            with self.assertRaises(PermissionError):connector.submit('liu','hello','forbidden')
            first=connector.submit('wan','hello','one')
            self.assertEqual(connector.submit('wan','hello','one')['id'],first['id'])
            with self.assertRaises(PermissionError):connector.submit('wan','hello again','two')
            self.assertNotEqual(actor.world,'default')
            self.assertEqual(self.store.conversations('owner')[0]['id'],'default')
            self.assertNotIn(actor.world,[c['id'] for c in self.store.conversations('owner')])
        finally:access.principal.reset(token)
        self.assertNotIn(created['token'],json.dumps(access.list_keys('owner')))
        with self.assertRaises(PermissionError):access.revoke('other',created['id'])
        access.revoke('owner',created['id']);self.assertIsNone(access.identify(created['token']))

    def test_mcp_transport_uses_official_json_rpc_and_rejects_cookies(self):
        with TestClient(server.app) as client:
            self.assertEqual(client.post('/mcp/',json={}).status_code,401)
            key=client.post('/api/mcp/keys',json={'label':'client'}).json()
            headers={'Authorization':'Bearer '+key['token'],'Accept':'application/json, text/event-stream'}
            initialized=client.post('/mcp/',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'regression','version':'1'}}})
            self.assertEqual(initialized.status_code,200,initialized.text)
            self.assertEqual(initialized.json()['result']['serverInfo']['name'],'Character Chat')
            tools=client.post('/mcp/',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}).json()
            self.assertIn('chat_with_character',[t['name'] for t in tools['result']['tools']])
            listing=client.post('/mcp/',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'list_characters','arguments':{}}})
            self.assertFalse(listing.json()['result'].get('isError',False))
            client.delete('/api/mcp/keys/'+key['id'])
            self.assertEqual(client.post('/mcp/',headers=headers,json={}).status_code,401)

    def test_clients_cannot_inspect_each_others_tasks(self):
        a=access.identify(access.create('owner','a',['read','chat','wan'])['token'])
        b=access.identify(access.create('owner','b',['read','chat','wan'])['token'])
        token=access.principal.set(a)
        try:run=connector.submit('wan','hello','request')
        finally:access.principal.reset(token)
        token=access.principal.set(b)
        try:
            connector.ensure_world(b)
            with self.assertRaises(PermissionError):connector.get_run(run['id'],'wan')
        finally:access.principal.reset(token)

    def test_langgraph_executes_once_and_persists_content_free_checkpoint(self):
        result=characters.send('owner','default','wan','private test content','graph-test')
        from backend.app.runtime.graph import execute
        calls=[]
        async def runner(identity):
            calls.append(identity)
            with Session(self.store.engine) as db:db.get(chat_inbox.Turn,identity).status='done';db.commit()
        asyncio.run(execute(result['id'],runner));asyncio.run(execute(result['id'],runner))
        self.assertEqual(calls,[result['id']])
        from langgraph.checkpoint.sqlite import SqliteSaver
        with SqliteSaver.from_conn_string(str(self.root/'chat.db')+'.checkpoints') as saver:
            states=list(saver.list(None));self.assertTrue(states)
            self.assertNotIn('private test content',str(states))

    def test_llamaindex_semantic_recall_caches_embeddings_without_text_copy(self):
        records=[{'text':'周末去植物园看兰花'},{'text':'厨房里有一袋面粉'}]
        client=MagicMock();client.__enter__.return_value=client
        def embed(**kwargs):return SimpleNamespace(data=[SimpleNamespace(embedding=[0.,1.] if '面粉' in value else [1.,0.]) for value in kwargs['input']],usage=SimpleNamespace(prompt_tokens=10,total_tokens=10))
        client.embeddings.create.side_effect=embed
        cfg={'embedding_model':'fake-embedding','api_key':'fake','base_url':'https://embedding.invalid/v1'}
        with patch('openai.OpenAI',return_value=client):
            ranked=retrieval.hybrid_rank('说好的赏花目的地',records,[],cfg)
            self.assertEqual(ranked[0][2],0)
            count=client.embeddings.create.call_count
            retrieval.hybrid_rank('说好的赏花目的地',records,[],cfg)
            self.assertEqual(client.embeddings.create.call_count,count)
            with patch.object(retrieval,'build_index',wraps=retrieval.build_index) as build:
                retrieval.hybrid_rank('说好的赏花目的地',records+[{'text':'另一个公开约定'}],[],cfg)
                build.assert_not_called()
        with Session(self.store.engine) as db:
            row=db.query(retrieval.Embedding).first();self.assertIsNotNone(row)
            self.assertNotIn('兰花',str(row.__dict__))
