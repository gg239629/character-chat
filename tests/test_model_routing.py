import unittest
from unittest.mock import patch
import test_home_settings as fixtures
import server,storage,actors,role_actions,model_usage


class ModelRoutingTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown
    def config_for_scope(self):
        user,cid=storage.scope.get()
        return {'model':cid,'api_key':'key-'+cid,'base_url':'https://'+cid+'.invalid'}
    def test_all_helper_categories_take_full_group_connection_in_private_scope(self):
        with role_actions.window('owner','default','yan'),patch.object(server,'load_config',side_effect=self.config_for_scope),patch.dict('os.environ',{'AUX_MODEL':'must-ignore'}):
            private=server.load_config()
            for category in ('coordinator','scene','archive'):
                resolved=model_usage.config(category,private)
                self.assertEqual(resolved['model'],'default')
                self.assertEqual(resolved['api_key'],'key-default')
                self.assertEqual(resolved['base_url'],'https://default.invalid')
            self.assertEqual(storage.scope.get()[1],actors.role_id('default','yan'))
    def test_every_agent_function_keeps_its_own_configuration(self):
        private={'model':'yan-model','api_key':'yan-key','base_url':'https://yan.invalid'}
        for category in ('character','action','initiative','initiative_public','peer_inquiry','peer_reply','peer_followup','action_voice'):
            self.assertEqual(model_usage.config(category,private),private)
    def test_suggestion_uses_wan_config_from_any_window(self):
        for channel in ('group','wan','yan','liu'):
            with role_actions.window('owner','default',channel),patch.object(server,'load_config',side_effect=self.config_for_scope):
                before=storage.scope.get()
                cfg=model_usage.config('suggestion',{'model':'ignored'})
                wan=actors.role_id('default','wan')
                self.assertEqual(cfg,{'model':wan,'api_key':'key-'+wan,'base_url':'https://'+wan+'.invalid'})
                self.assertEqual(storage.scope.get(),before)
    def test_helpers_do_not_mix_story_or_owner(self):
        self.store.ensure('another','story-b',{})
        with role_actions.window('another','story-b','liu'),patch.object(server,'load_config',side_effect=self.config_for_scope):
            self.assertEqual(model_usage.group_config()['model'],'story-b')
    def test_missing_group_configuration_does_not_fall_back_to_actor(self):
        with role_actions.window('owner','default','wan'),patch.object(server,'load_config',return_value={'model':'','api_key':''}):
            self.assertEqual(model_usage.config('scene',{'model':'wan','api_key':'key'})['model'],'')
