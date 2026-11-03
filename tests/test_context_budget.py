import unittest
from backend.app.characters.context_budget import needs_recall,select_profile,fit

class ContextBudgetTests(unittest.TestCase):
    def test_plain_continuation_skips_extra_retrieval_but_recall_and_contact_do_not(self):
        for text in ['好呀','早上好','你在干嘛','嗯嗯']:
            self.assertFalse(needs_recall(text))
        for text in ['还记得上次的约定吗','让她给我发消息','你们昨天去哪里了']:
            self.assertTrue(needs_recall(text))
    def test_profile_budget_keeps_identity_and_relevant_detail(self):
        text='身份：测试人物。\n\n性格：开朗。\n\n植物园里喜欢兰花。\n\n'+'杂项记录。'*1000
        result=select_profile(text,'植物园',100)
        self.assertIn('测试人物',result);self.assertIn('兰花',result);self.assertLessEqual(len(result),110)
    def test_payload_keeps_latest_user_explicit_facts_and_persona(self):
        data=[{'role':'system','content':'核心身份'}, {'role':'assistant','content':'旧聊天'*200},
          {'role':'system','content':'明确设定与空间依据：用户住在外地'},
          {'role':'user','content':'最新的问题'}, {'role':'system','content':'你是测试人物，独立自然。'}]
        result=fit(data,100)
        self.assertIn(data[-1],result);self.assertIn(data[-2],result);self.assertIn(data[2],result)
        self.assertNotIn(data[1],result)

    def test_dialogue_window_expands_only_for_followups_or_unfinished_work(self):
        from backend.app.characters.context_budget import dialogue_window
        messages=[]
        for i in range(12):
            messages.extend([{'role':'user','content':str(i)},{'role':'assistant','content':'回答'}])
        messages.append({'role':'user','content':'早上好'})
        self.assertEqual(sum(m['role']=='user' for m in dialogue_window(messages)),4)
        for text in ['继续刚才的话题','让她在群里发消息','大家分别说说','为什么这样']:
            with self.subTest(text=text):
                messages[-1]['content']=text
                self.assertEqual(sum(m['role']=='user' for m in dialogue_window(messages)),8)
        messages[-1]['content']='好';messages[-2]['content']='我正在处理，稍后告诉你'
        self.assertEqual(sum(m['role']=='user' for m in dialogue_window(messages)),8)

    def test_dialogue_budget_removes_whole_old_rounds_and_preserves_current_input(self):
        from backend.app.characters.context_budget import dialogue_window
        messages=[]
        for i in range(8):messages.extend([{'role':'user','content':str(i)},{'role':'assistant','content':'字'*1500}])
        messages.append({'role':'user','content':'早上好'})
        selected=dialogue_window(messages)
        self.assertLessEqual(sum(len(m['content']) for m in selected),3500)
        self.assertEqual(selected[-1],messages[-1]);self.assertEqual(selected[0]['role'],'user')
