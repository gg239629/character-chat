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


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store('sqlite:///' + (Path(self.tmp.name)/'chat.db').as_posix())
        self.patcher = patch.object(storage, '_STORE', self.store)
        self.patcher.start()
        self.store.ensure('owner', 'default', {'scene_state': {}, 'recent_log': []})

    def tearDown(self):
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
            self.assertTrue(long_memory.recall('Alice的兰花约定'))
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
        self.assertEqual(len(storage.read_document('archive.json', [])), 20)
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
                new_id = client.post('/api/conversations').json()['id']
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

    def test_import_does_not_overwrite_database(self):
        storage.write_document('scene_state', {'地点': '已迁移'})
        storage.initialize()
        self.assertEqual(storage.memory_pack()['scene_state'], {'地点': '已迁移'})

if __name__ == '__main__':
    unittest.main()
