import unittest
from pathlib import Path
from unittest.mock import patch
from sqlalchemy.orm import Session
import test_home_settings as fixtures
import server; from backend.app.infrastructure import storage; from backend.app.characters import model_defaults; from backend.app.runtime import role_actions
from fastapi.testclient import TestClient
REAL_LOAD=server.load_config

class DefaultsTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown
    def test_all_channels_inherit_then_override_and_reset_inheritance(self):
        with patch.object(server,'load_config',side_effect=REAL_LOAD),patch.object(server,'CONFIG_FILE',self.root/'absent'),TestClient(server.app) as client:
            response=client.put('/api/home/model',json={'model':'global','base_url':'https://global.invalid/v1','api_key':'private-test-key'})
            self.assertEqual(response.status_code,200)
            self.assertNotIn('private-test-key',response.text)
            for channel in ('group','wan','yan','liu'):
                settings=client.get('/api/settings',headers={'X-Chat-Channel':channel}).json()
                self.assertEqual(settings['effective']['model'],'global');self.assertTrue(settings['inherit_model'])
            client.post('/api/settings',headers={'X-Chat-Channel':'yan'},json={'model':'own','inherit_model':False})
            client.put('/api/home/model',json={'model':'global2','base_url':'https://global.invalid/v1'})
            self.assertEqual(client.get('/api/settings',headers={'X-Chat-Channel':'yan'}).json()['effective']['model'],'own')
            self.assertEqual(client.get('/api/settings').json()['effective']['model'],'global2')
            client.post('/api/settings',headers={'X-Chat-Channel':'yan'},json={'inherit_model':True})
            self.assertEqual(client.get('/api/settings',headers={'X-Chat-Channel':'yan'}).json()['effective']['model'],'global2')
            with Session(self.store.engine) as db:
                self.assertTrue(db.get(model_defaults.Defaults,'owner').settings['api_key'].startswith('fernet:v1:'))
    def test_global_model_is_user_scoped_and_persona_save_does_not_freeze_inheritance(self):
        model_defaults.save('owner',{'model':'owner','api_key':'key'})
        self.assertEqual(model_defaults.read('another'),{})
        with patch.object(server,'load_config',side_effect=REAL_LOAD),TestClient(server.app) as client:
            client.post('/api/settings',headers={'X-Chat-Channel':'wan'},json={'persona':'直率'})
            self.assertTrue(client.get('/api/settings',headers={'X-Chat-Channel':'wan'}).json()['inherit_model'])
    def test_global_save_makes_legacy_values_inherit_until_explicit_override(self):
        with role_actions.window('owner','default','wan'):
            storage.write_document('settings',{'model':'legacy','api_key':'old'})
        model_defaults.save('owner',{'model':'new-global','api_key':'new'})
        with role_actions.window('owner','default','wan'),patch.object(server,'load_config',side_effect=REAL_LOAD):
            self.assertEqual(server.load_config()['model'],'new-global')
