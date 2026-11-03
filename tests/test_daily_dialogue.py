import unittest
from backend.app.characters import reply_quality

class DailyDialogueTests(unittest.TestCase):
    def test_repeated_status_removed_but_new_question_and_action_remain(self):
        history=[{'role':'assistant','content':'小研：还没吃上呢。小婉在煮玉米。'}]
        text='小研：还没吃上。\n（把书放到床边）\n蛋还要几个？'
        result=reply_quality.trim_repeated(text,history)
        self.assertNotIn('还没吃上',result)
        self.assertIn('把书放到床边',result);self.assertIn('蛋还要几个？',result)
    def test_changed_status_and_quantities_not_removed(self):
        history=[{'role':'assistant','content':'小研：还没吃上。蛋三个够了。'}]
        result=reply_quality.trim_repeated('小研：已经吃上了。蛋四个才够。',history)
        self.assertIn('已经吃上',result);self.assertIn('四个',result)
    def test_other_roles_may_answer_same_question_in_their_own_right(self):
        history=[{'role':'assistant','content':'小婉：我还没吃饭。'}]
        self.assertIn('我还没吃饭',reply_quality.trim_repeated('小研：我还没吃饭。',history))
    def test_all_repeated_speech_becomes_silence(self):
        history=[{'role':'assistant','content':'小研：还没吃上呢。'}]
        self.assertEqual(reply_quality.trim_repeated('小研：还没吃上。',history),'')
    def test_user_reasking_can_receive_same_factual_answer(self):
        history=[{'role':'assistant','content':'小研：还没吃上。'},{'role':'user','content':'现在吃上了吗？'}]
        self.assertIn('还没吃上',reply_quality.trim_repeated('小研：还没吃上。',history))
