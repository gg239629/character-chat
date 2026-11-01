import unittest
from unittest.mock import patch,MagicMock
from types import SimpleNamespace as S
import json
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import role_actions
import scene_labels
import server
import storage
import world_events


class ArrivalTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_arrival_updates_before_prompt_and_does_not_move_other_people(self):
        self.store.mutate('owner','default',lambda d:d.update(scene_state={'小婉':'路上·开车','小研':'厨房·做饭'}))
        with role_actions.window('owner','default','wan'):
            world_events.capture('owner','default','wan',{'id':'arrival','role':'user','content':'我们现在到家啦','created_at':100})
            self.assertEqual(server.displayed_scene()['小婉'],'家门口·刚到家')
            self.assertEqual(server.displayed_scene()['小研'],'厨房·做饭')
            _,payload=server.actor_payload('wan',[{'role':'user','content':'我们现在到家啦'}],False)
            self.assertNotIn('路上·开车',json.dumps(payload,ensure_ascii=False))

    def test_stopping_and_entering_home_are_action_evidence(self):
        cases=[('小婉：（把车停稳，拉手刹，熄了火）','车内·已停车'),
            ('小婉：（换了鞋，低头往厨房那头看一眼）','玄关·换鞋')]
        for text,status in cases:
            messages=[{'role':'assistant','content':text}]
            self.assertTrue(scene_labels.has_action_evidence(messages,'小婉'))
            self.assertEqual(scene_labels.physical_transition(messages,'小婉'),status)

    def test_plans_lies_and_questions_do_not_move_character(self):
        for text in ('我到家啦','我现在到家了','我们还没到家','我们明天到家','我们准备到家以后吃饭','我们到家了吗？','她说“到家啦”'):
            self.assertIsNone(scene_labels.physical_transition([{'role':'user','content':text}],'小婉',True),text)
        self.assertIsNone(scene_labels.physical_transition([{'role':'assistant','content':'小婉：我在家了。'}],'小婉'))

    def test_old_record_cannot_overwrite_newer_home_evidence(self):
        world_events.capture('owner','default','wan',{'id':'home','role':'user','content':'我们到家啦','created_at':200})
        world_events.capture('owner','default','wan',{'id':'old','role':'assistant','speaker':'小婉',
            'content':'小婉：（把车停稳，熄火）','created_at':100})
        self.assertEqual(self.store.read('owner','default')['role_states']['wan']['status'],'家门口·刚到家')

    def test_model_cannot_keep_driving_after_explicit_completed_arrival(self):
        self.store.mutate('owner','default',lambda d:d.update(scene_state={'小婉':'路上·开车'}))
        model=MagicMock();model.__enter__.return_value=model
        model.chat.completions.create.return_value=S(choices=[S(message=S(content=json.dumps({'小婉':{'地点':'路上','动作':'开车'}})))])
        with role_actions.window('owner','default','wan'),patch('openai.OpenAI',return_value=model):
            state=server.update_scene_state([{'role':'user','content':'我们到家啦'}],cfg_override={'api_key':'fake','model':'fake'})
            self.assertEqual(state['小婉'],'家门口·刚到家')
