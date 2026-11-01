import unittest
import test_home_settings as fixtures
import continuity,reply_quality,scene_labels,world_time

class ContinuityTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown
    def record(self,text,role='user',speaker='',stamp=100):
        return dict(id=str(stamp),content=text,role=role,speaker=speaker,created_at=stamp)
    def test_explicit_identity_is_saved_and_hypotheses_do_not_replace_it(self):
        continuity.capture('owner','default','wan',self.record('我是男的呀'))
        continuity.capture('owner','default','wan',self.record('如果我是女性呢？',stamp=101))
        self.assertEqual(continuity.context('owner','default','wan')['user_profile']['gender'],'男')
    def test_plans_do_not_move_people(self):
        continuity.capture('owner','default','wan',self.record('小婉：（走进卧室）','assistant','小婉'))
        continuity.capture('owner','default','wan',self.record('小婉：（正要走进厨房）','assistant','小婉',101))
        self.assertEqual(continuity.context('owner','default','wan')['own_position']['place'],'卧室')
    def test_bowl_does_not_imply_kitchen(self):
        status=scene_labels.ground_location([self.record('小柳：（端起碗）','assistant','小柳')],'小柳','厨房·吃饭','卧室·坐着')
        self.assertEqual(status,'卧室·吃饭')
        self.assertEqual(scene_labels.ground_location([self.record('小柳：（走进厨房）','assistant','小柳')],'小柳','厨房·拿碗','卧室·坐着'),'厨房·拿碗')
    def test_routine_does_not_teleport_active_role(self):
        data={'story_clock':{'day':1,'minute':700},'scene_state':{'小柳':'卧室·看书'}}
        world_time.apply(data,{'day':1,'minute':730},{'liu'})
        self.assertEqual(data['role_states']['liu']['status'],'卧室·看书')
    def test_repeat_and_empty_acknowledgment_stop(self):
        self.assertTrue(reply_quality.redundant('小研：我们九点出门。',['小婉：我们九点出门。']))
        self.assertFalse(reply_quality.redundant('小研：我们十点出门？',['小婉：我们九点出门。']))
        self.assertTrue(reply_quality.redundant('小柳：嗯。',['好。','知道了。']))
