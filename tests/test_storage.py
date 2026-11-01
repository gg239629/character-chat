"""Run with: python -m unittest discover -s tests -v"""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import storage
import server
import long_memory
import secrets_store
from cryptography.fernet import Fernet


class StorageTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import AsyncMock
        import actors
        self.coordinator_patch=patch.object(server.group_coordinator,'plan',new=AsyncMock(side_effect=lambda messages,configs:[role for role in actors.candidates(messages) if not any(m.get('role')=='assistant' and m.get('content','').startswith(actors.ROLES[role]+'：') for m in server.recent_rounds(messages,1))]))
        self.coordinator_patch.start()
        self.addCleanup(self.coordinator_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store('sqlite:///' + (Path(self.tmp.name)/'chat.db').as_posix())
        self.patcher = patch.object(storage, '_STORE', self.store)
        self.patcher.start()
        self.secret_patch = patch.dict('os.environ', {'RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY': Fernet.generate_key().decode()})
        self.secret_patch.start()
        self.store.ensure('owner', 'default', {'scene_state': {}, 'recent_log': []})

    def tearDown(self):
        self.secret_patch.stop()
        self.patcher.stop()
        self.store.engine.dispose()
        self.tmp.cleanup()

    def test_owner_and_conversation_isolation(self):
        self.store.ensure('alice', 'alice-room')
        self.store.ensure('bob', 'bob-room')
        token = storage.scope.set(('alice', 'alice-room'))
        try:
            storage.append_log([{'who': '他', 'text': '只属于Alice的兰花约定'}])
            storage.write_document('scene_state', {'地点': '公园'})
            storage.write_document('summary.json', {'text': 'Alice summary', 'cursor': 1})
            self.assertTrue(long_memory.recall('Alice的兰花约定')[1])
        finally:
            storage.scope.reset(token)
        token = storage.scope.set(('bob', 'bob-room'))
        try:
            self.assertEqual(storage.memory_pack()['recent_log'], [])
            self.assertEqual(long_memory.context('兰花约定'), '')
        finally:
            storage.scope.reset(token)
        with self.assertRaises(PermissionError):
            self.store.read('bob', 'alice-room')
        with self.assertRaises(PermissionError):
            self.store.ensure('bob', 'alice-room')

    def test_concurrent_append_and_scene_update(self):
        def append(i):
            storage.append_log([{'who': '他', 'text': str(i)}])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(20)))
        storage.write_document('scene_state', {'地点': '客厅'})
        pack = storage.memory_pack()
        self.assertEqual(len(pack['recent_log']), 20)
        self.assertEqual(storage.read_document('pending_archive', []), [])
        self.assertEqual(pack['scene_state']['地点'], '客厅')
        reopened = storage.Store('sqlite:///' + (Path(self.tmp.name)/'chat.db').as_posix())
        try:
            self.assertEqual(len(reopened.read('owner', 'default')['recent_log']), 20)
        finally:
            reopened.engine.dispose()

    def test_api_scope_settings_and_export(self):
        with patch.object(server, 'initialize_database', lambda: None):
            # Startup uses the existing idempotent importer; default row already exists.
            with TestClient(server.app) as client:
                new_id = 'isolated-story'
                self.store.ensure('owner', new_id)
                headers = {'X-Conversation-ID': new_id}
                self.assertEqual(client.get('/api/memory/export', headers=headers).json(), {'scene_state': {}, 'recent_log': []})
                with patch.object(server, 'update_scene_state', return_value=None), patch.object(long_memory, 'refresh_summary'):
                    result = client.post('/api/save', headers=headers, json={'messages': [{'role': 'user', 'content': '独立会话内容'}]})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()['saved'], 1)
                self.assertEqual(client.get('/api/memory/export').json()['recent_log'], [])
                self.assertEqual(client.get('/api/memory/export', headers=headers).json()['recent_log'][0]['text'], '独立会话内容')
                self.assertEqual(client.get('/api/memory/export', headers={'X-Conversation-ID': 'nonexistent'}).status_code, 404)
                self.store.ensure('other', 'private-room')
                self.assertEqual(client.get('/api/memory/export', headers={'X-Conversation-ID': 'private-room'}).status_code, 404)
                client.post('/api/settings', headers=headers, json={'api_key': 'fake-test-key', 'model': 'isolated-test'})
                settings = client.get('/api/settings', headers=headers).json()
                self.assertTrue(settings['effective']['has_key'])
                self.assertNotIn('fake-test-key', json.dumps(settings))
                self.assertNotEqual(client.get('/api/settings').json()['effective']['model'], 'isolated-test')

    def test_story_home_and_named_empty_story(self):
        with TestClient(server.app) as client:
            self.assertIn('我的会话', client.get('/').text)
            self.assertIn('返回主页', client.get('/chat').text)
            story = 'existing-story'
            self.store.ensure('owner', story, {'title': '雨天书店'})
            self.assertEqual(client.post('/api/conversations',json={'title':'not-created'}).status_code,405)
            self.assertNotIn('id="create"',client.get('/').text)
            items = client.get('/api/conversations').json()
            self.assertTrue(any(item['id']==story and item['title']=='雨天书店' and item['avatar']=='' for item in items))
            headers = {'X-Conversation-ID': story}
            self.assertEqual(client.get('/api/memory/export', headers=headers).json(),
                             {'scene_state': {}, 'recent_log': []})
            self.store.mutate('owner', story, lambda data: data.update(scene_state={'地点': '书店'}))
            self.assertEqual(client.get('/api/info', headers=headers).json()['scene_state'], {'地点': '书店'})
            self.assertNotEqual(client.get('/api/info').json()['scene_state'], {'地点': '书店'})

    def test_profile_edit_is_isolated(self):
        with TestClient(server.app) as client:
            identity = 'profile-story'
            self.store.ensure('owner', identity)
            headers = {'X-Conversation-ID': identity}
            response = client.put('/api/conversation/profile', headers=headers, json={'title': '新名字', 'avatar': ''})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['title'], '新名字')
            avatar = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7WQAAAAASUVORK5CYII='
            response = client.put('/api/conversation/profile',headers=headers,json={'title':'新名字','avatar':avatar})
            self.assertEqual(response.json()['avatar'], avatar)
            self.assertEqual(client.get('/api/conversation/profile',headers=headers).json()['avatar'],avatar)
            self.assertEqual(client.get('/api/conversation/profile').json()['avatar'],'')
            self.assertNotEqual(client.get('/api/conversation/profile').json()['title'], '新名字')
            for data in ({'title': ''}, {'title': 'x'*61}, {'title':'正常','avatar':'data:image/svg+xml;base64,xxx'}):
                self.assertEqual(client.put('/api/conversation/profile',headers=headers,json=data).status_code,422)

    def test_private_channels_isolate_memory_and_share_public_world(self):
        self.store.mutate('owner','default',lambda data:data.update(scene_state={'地点':'客厅'},public_history=[{'role':'user','content':'公开约定周末看电影'}]))
        with TestClient(server.app) as client:
            with patch.object(server,'update_scene_state'),patch.object(long_memory,'refresh_summary'):
                for channel, secret in (('wan','红色秘密'),('yan','蓝色秘密'),('liu','绿色秘密')):
                    headers={'X-Chat-Channel':channel}
                    response=client.post('/api/save',headers=headers,json={'messages':[{'role':'user','content':secret}]})
                    self.assertEqual(response.status_code,200)
                    pack=client.get('/api/memory/export',headers=headers).json()
                    self.assertEqual([m['text'] for m in pack['recent_log']],[secret])
                    self.assertEqual(client.get('/api/info',headers=headers).json()['scene_state']['地点'],'客厅')
                self.assertEqual(client.get('/api/memory/export').json()['recent_log'],[])
                self.assertEqual(len(client.get('/api/conversations').json()),1)
                self.assertEqual(client.get('/api/info',headers={'X-Chat-Channel':'invalid'}).status_code,400)
            captured=[]
            original=server.actor_payload
            def inspect(role,messages,group):
                cfg,payload=original(role,messages,group);captured.extend(payload);return cfg,payload
            with patch.object(server,'actor_payload',side_effect=inspect),patch.object(server,'make_client',side_effect=RuntimeError('stop before network')):
                with TestClient(server.app,raise_server_exceptions=False) as probe:
                    probe.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={'messages':[{'role':'user','content':'你们好'}]})
            text=json.dumps(captured,ensure_ascii=False)
            self.assertIn('蓝色秘密',text)
            self.assertIn('公开约定周末看电影',text)
            self.assertIn('你只扮演小研',text)
            self.assertNotIn('红色秘密',text)
            self.assertNotIn('绿色秘密',text)

    def test_channel_profiles_settings_and_group_avatars(self):
        avatar='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7WQAAAAASUVORK5CYII='
        with TestClient(server.app) as client:
            for channel in ('group','wan','yan','liu'):
                headers={'X-Chat-Channel':channel}
                self.assertEqual(client.post('/api/settings',headers=headers,json={'api_key':'fake-'+channel,'model':'model-'+channel}).status_code,200)
                response=client.put('/api/conversation/profile',headers=headers,json={'title':'会话-'+channel,'avatar':avatar if channel=='wan' else ''})
                self.assertEqual(response.status_code,200)
            for channel in ('group','wan','yan','liu'):
                headers={'X-Chat-Channel':channel}
                self.assertEqual(client.get('/api/settings',headers=headers).json()['effective']['model'],'model-'+channel)
                self.assertEqual(client.get('/api/conversation/profile',headers=headers).json()['title'],'会话-'+channel)
            info=client.get('/api/info').json()
            self.assertEqual(info['role_avatars']['小婉'],avatar)
            self.assertEqual(info['role_avatars']['小研'],'')
            items=client.get('/api/conversations').json()
            self.assertEqual(len(items),1)
            self.assertEqual(items[0]['channels']['wan']['avatar'],avatar)
            self.assertEqual(items[0]['channels']['yan']['title'],'会话-yan')

    def test_stream_publishes_only_group_history(self):
        from types import SimpleNamespace
        async def create(**kwargs):
            content='小研：周六看电影。' if '你只扮演小研' in kwargs['messages'][-1]['content'] else '[[SILENT]]'
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])
            return chunks()
        client_model=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        cfg={'api_key':'fake','model':'test','temperature':.2,'max_tokens':100}
        with TestClient(server.app) as client,patch.object(server,'load_config',return_value=cfg),patch.object(server,'make_client',return_value=client_model),patch.object(server,'update_scene_state'):
            response=client.post('/api/stream',json={'messages':[{'role':'user','content':'小研，公开电影计划'}]})
            self.assertIn('"done": true',response.text)
            import memory_autosave
            memory_autosave.flush('owner','default',memory_autosave.checkpoint_due('owner','default'))
            public=self.store.read('owner','default')['public_history']
            self.assertIn('公开电影计划',json.dumps(public,ensure_ascii=False))
            response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={'messages':[{'role':'user','content':'私密礼物'}]})
            self.assertIn('"done": true',response.text)
            self.assertEqual(self.store.read('owner','default')['public_history'],public)

    def test_request_snapshot_avoids_repeat_reads(self):
        snapshot = storage.load_request_data('owner', 'default')
        token = storage.request_snapshot.set(snapshot)
        try:
            with patch.object(self.store, 'read', wraps=self.store.read) as reads:
                storage.memory_pack()
                storage.read_document('settings', {})
                storage.read_document('long_summaries', [])
                self.assertEqual(reads.call_count, 0)
                storage.write_document('scene_state', {'地点': '书房'})
                self.assertEqual(storage.memory_pack()['scene_state']['地点'], '书房')
                self.assertEqual(reads.call_count, 0)
            self.store.ensure('other', 'other-room')
            scope_token = storage.scope.set(('other', 'other-room'))
            try:
                self.assertEqual(storage.memory_pack()['scene_state'], {})
            finally:
                storage.scope.reset(scope_token)
        finally:
            storage.request_snapshot.reset(token)

    def test_import_does_not_overwrite_database(self):
        storage.write_document('scene_state', {'地点': '已迁移'})
        storage.initialize()
        self.assertEqual(storage.memory_pack()['scene_state'], {'地点': '已迁移'})

class StartupPathTests(unittest.TestCase):
    def test_postgres_does_not_create_local_directory(self):
        with patch.object(storage, '_STORE', None), patch.dict('os.environ', {'DATABASE_URL': 'postgresql://test.invalid/db', 'RENDER': 'true'}), patch.object(Path, 'mkdir', side_effect=PermissionError('read-only filesystem')) as mkdir, patch.object(storage, 'Store') as constructor:
            self.assertIs(storage.get_store(), constructor.return_value)
            mkdir.assert_not_called()
            constructor.assert_called_once_with('postgresql://test.invalid/db')

    def test_render_without_database_fails_before_local_write(self):
        with patch.object(storage, '_STORE', None), patch.dict('os.environ', {'DATABASE_URL': '', 'RENDER': 'true'}), patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaisesRegex(RuntimeError, 'DATABASE_URL'):
                storage.get_store()
            mkdir.assert_not_called()

if __name__ == '__main__':
    unittest.main()
