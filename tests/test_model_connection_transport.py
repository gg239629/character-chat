"""Both website connection-test routes must reach a local gateway directly."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import test_home_settings as fixtures
from test_model_transport import endpoints
import server


class ModelConnectionTransportTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_default_model_connection_test_with_environment_proxy(self):
        with endpoints() as (url,calls),TestClient(server.app) as client:
            response=client.post('/api/home/model/test',json={
                'model':'test','base_url':url,'api_key':'test-only'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(response.json()['ok'])
            self.assertEqual(calls['gateway'],['/v1/chat/completions'])
            self.assertEqual(calls['proxy'],[])

    def test_channel_model_connection_test_with_environment_proxy(self):
        with endpoints() as (url,calls),TestClient(server.app) as client:
            with patch.object(server,'load_config',return_value={
                'model':'test','api_key':'test-only','base_url':url}):
                response=client.post('/api/test',headers={'X-Chat-Channel':'yan'},json={})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['reply'],'好')
            self.assertEqual(calls['gateway'],['/v1/chat/completions'])
            self.assertEqual(calls['proxy'],[])
