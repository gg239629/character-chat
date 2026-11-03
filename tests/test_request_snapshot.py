"""Request-local reuse reduces reads without caching across users or writes."""
import unittest
from unittest.mock import Mock, patch
import server
from backend.app.infrastructure import storage


class RequestSnapshotTests(unittest.TestCase):
    def check_read(self, read_only, channel):
        store = Mock()
        store.read.return_value = {'source': 'database'}
        world_token = server.chat_world.set({'id': 'world', 'channel': channel,
                                             'read_only': read_only, 'data': {'source': 'world'}})
        scope_token = storage.scope.set(('user', 'world' if channel == 'group' else 'private'))
        snapshot_token = storage.request_snapshot.set({'scope': storage.scope.get(),
                                                       'data': {'source': 'snapshot'}})
        try:
            with patch.object(storage, 'get_store', return_value=store):
                result = server.shared_world_data()
            if read_only:
                store.read.assert_not_called()
                self.assertEqual(result['source'], 'snapshot' if channel == 'group' else 'world')
            else:
                store.read.assert_called_once_with('user', 'world')
                self.assertEqual(result['source'], 'database')
        finally:
            storage.request_snapshot.reset(snapshot_token)
            storage.scope.reset(scope_token)
            server.chat_world.reset(world_token)

    def test_group_get_reuses_snapshot(self):
        self.check_read(True, 'group')

    def test_private_get_reuses_world_not_private_snapshot(self):
        self.check_read(True, 'wan')

    def test_write_request_still_reads_current_world(self):
        self.check_read(False, 'wan')
