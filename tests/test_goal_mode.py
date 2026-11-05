import asyncio
import unittest
from unittest.mock import AsyncMock,patch
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import server
from backend.app.infrastructure import storage
from backend.app.runtime import group_coordinator,goal_mode,role_actions

class GoalModeTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_group_toggle_defaults_off_and_private_cannot_edit(self):
        with TestClient(server.app) as client:
            self.assertFalse(client.get('/api/settings').json()['goal_mode'])
            self.assertTrue(client.post('/api/settings',json={'goal_mode':True}).json()['goal_mode'])
            self.assertEqual(client.post('/api/settings',headers={'X-Chat-Channel':'liu'},json={'goal_mode':False}).status_code,422)
            self.assertTrue(client.get('/api/settings').json()['goal_mode'])

    def test_checks_each_reply_and_stops_at_goal_without_public_result(self):
        self.store.mutate('owner','default',lambda data:data.update(goal_mode=True))
        history=[{'role':'user','content':'商量好几点出发'}];seen=[]
        decisions=[goal_mode.Decision(goal='确定出发时间',status='continue',order=['wan']),
            goal_mode.Decision(goal='试图换目标',status='continue',order=['yan']),
            goal_mode.Decision(goal='确定出发时间',status='achieved',evidence='九点出发')]
        async def judge(goal,topic,messages,state):
            seen.append((goal,len(messages)))
            return decisions[len(seen)-1]
        async def run():
            result=[]
            async for actor in group_coordinator.speakers(history,{}):
                self.assertEqual(goal_mode.active_goal.get(),'确定出发时间')
                result.append(actor);history.append({'role':'assistant','content':('小婉：九点行吗？' if actor=='wan' else '小研：好，九点出发。')})
            return result
        with role_actions.window('owner','default','group'),patch.object(goal_mode,'judge',new=judge),patch.object(group_coordinator,'plan',new=AsyncMock()) as normal:
            self.assertEqual(asyncio.run(run()),['wan','yan'])
            normal.assert_not_called()
        self.assertEqual(seen,[('',1),('确定出发时间',2),('确定出发时间',3)])
        self.assertEqual(len(history),3)
        self.assertEqual(goal_mode.active_goal.get(),'')

    def test_goal_cannot_be_marked_complete_without_assistant_evidence(self):
        answer={'goal':'确定时间','status':'achieved','order':[],'evidence':'已经确定'}
        with role_actions.window('owner','default','group'),patch.object(role_actions,'ask_json',new=AsyncMock(return_value=answer)):
            with self.assertRaises(ValueError):asyncio.run(goal_mode.judge('','确定时间',[{'role':'user','content':'已经确定'}],{}))

    def test_model_only_receives_current_public_exchange(self):
        answer={'goal':'选时间','status':'continue','order':['yan'],'evidence':''}
        history=[{'role':'user','content':'旧话题'},{'role':'assistant','content':'旧答复'},
                 {'role':'user','content':'新目标选时间'}]
        with role_actions.window('owner','default','group'),patch.object(role_actions,'ask_json',new=AsyncMock(return_value=answer)) as ask:
            result=asyncio.run(goal_mode.judge('','新目标选时间',history,{}))
            self.assertEqual(result.status,'continue')
            self.assertEqual(len(ask.call_args.args[2]['group_exchange']),1)
            self.assertNotIn('private',str(ask.call_args.args[2]))

    def test_needs_user_input_stops_without_claiming_goal_achieved(self):
        self.store.mutate('owner','default',lambda data:data.update(goal_mode=True))
        answers=[goal_mode.Decision(goal='选餐厅',status='continue',order=['liu']),
                 goal_mode.Decision(goal='选餐厅',status='needs_input')]
        history=[{'role':'user','content':'帮我选餐厅'}]
        async def run():
            result=[]
            async for actor in group_coordinator.speakers(history,{}):
                result.append(actor);history.append({'role':'assistant','content':'小柳：你想去哪一带？'})
            return result
        with role_actions.window('owner','default','group'),patch.object(goal_mode,'judge',new=AsyncMock(side_effect=answers)):
            self.assertEqual(asyncio.run(run()),['liu'])
