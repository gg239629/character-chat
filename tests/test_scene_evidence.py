import unittest
from backend.app.world import scene_labels


class SceneEvidenceTests(unittest.TestCase):
    def test_claims_deception_plans_and_commands_are_not_actions(self):
        for role,text in [('assistant','小婉：我在外面跑步。'),('assistant','小婉：我在跑步！（其实没动，只是撒谎）'),('user','小婉说自己在厨房'),('user','小婉去厨房吧'),('user','小婉准备走进厨房'),('user','小婉在厨房吗？')]:
            self.assertFalse(scene_labels.has_action_evidence([{'role':role,'content':text}],'小婉'),text)

    def test_only_actual_actor_actions_authorize_change(self):
        messages=[{'role':'assistant','content':'小婉：（走进厨房，开始煮粥）早饭喝粥。'}]
        self.assertTrue(scene_labels.has_action_evidence(messages,'小婉'))
        self.assertFalse(scene_labels.has_action_evidence(messages,'小研'))
        self.assertTrue(scene_labels.has_action_evidence([{'role':'user','content':'小婉回到卧室看书'}],'小婉'))
