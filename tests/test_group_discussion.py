import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import actors
import chat_history
import group_coordinator as coordinator
import server
import storage


class DiscussionFlowTests(unittest.TestCase):
    def test_replans_after_reply_allows_previous_speaker_and_stops_naturally(self):
        history=[{'role':'user','content':'大家聊聊'}];seen=[]
        async def plan(messages,configs):
            seen.append((len(messages),dict(coordinator.discussion_state.get())))
            return [['wan','liu'],['yan'],['wan'],[]][len(seen)-1]
        async def run():
            result=[]
            async for role in coordinator.speakers(history,{}):
                result.append(role);history.append({'role':'assistant','content':actors.ROLES[role]+'：新观点'+str(len(result))})
            return result
        with patch.object(coordinator,'plan',new=plan):self.assertEqual(asyncio.run(run()),['wan','yan','wan'])
        self.assertEqual([n for n,_ in seen],[1,2,3,4])
        self.assertEqual(seen[2][1]['last_speaker'],'yan')
        self.assertIsNone(coordinator.discussion_state.get())

    def test_silent_role_can_reconsider_after_someone_else_speaks(self):
        history=[]
        async def run():
            result=[]
            async for role in coordinator.speakers(history,{}):
                result.append(role)
                if len(result)>1:history.append({'role':'assistant','content':'new'})
            return result
        with patch.object(coordinator,'plan',new=AsyncMock(side_effect=[['wan','yan'],['wan'],[]])):
            self.assertEqual(asyncio.run(run()),['wan','yan','wan'])

    def test_no_progress_stops_and_bad_model_cannot_loop_forever(self):
        async def run(append):
            history=[];result=[]
            async for role in coordinator.speakers(history,{}):
                result.append(role)
                if append:history.append({'role':'assistant','content':'new'})
            return result
        with patch.object(coordinator,'plan',new=AsyncMock(return_value=['wan','yan','liu'])):
            self.assertEqual(asyncio.run(run(False)),['wan','yan','liu'])
            self.assertEqual(len(asyncio.run(run(True))),coordinator.MAX_MESSAGES)


class DiscussionIntegrationTests(unittest.TestCase):
    def test_same_actor_answers_peer_after_own_first_reply_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=storage.Store('sqlite:///'+(Path(tmp)/'test.db').as_posix());store.ensure('owner','default',{'recent_log':[]})
            texts=['小婉：我想去植物园。','小研：小婉，你想几点出发？','小婉：小研，九点怎么样？']
            expected=['wan','yan','wan'];calls=[];snapshots=[]
            async def plan(messages,configs):
                snapshots.append([dict(m) for m in messages])
                n=len(calls)
                return [expected[n]] if n<len(expected) else []
            def model(cfg):
                async def create(**kwargs):
                    n=len(calls);payload=kwargs['messages'];name=actors.ROLES[expected[n]]
                    self.assertTrue(any('你只扮演'+name in m['content'] for m in payload if m['role']=='system'))
                    for previous in texts[:n]:
                        self.assertIn(previous,json.dumps(payload,ensure_ascii=False))
                        self.assertTrue(any(m['content']==previous for m in chat_history.messages()))
                    calls.append(name)
                    async def chunks():yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=texts[n]))])
                    return chunks()
                async def close():pass
                return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
            try:
                with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value={'api_key':'fake','model':'test','base_url':'','temperature':.5,'max_tokens':200}),patch.dict('os.environ',{'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()}),patch.object(coordinator,'plan',new=plan),patch.object(server,'make_client',side_effect=model),TestClient(server.app) as client:
                    body={'request_id':'peer-conversation','messages':[{'role':'user','content':'周末一起去哪儿？'}]}
                    response=client.post('/api/stream',json=body)
                    self.assertIn('"done": true',response.text);self.assertNotIn('"error":',response.text)
                    messages=client.get('/api/messages').json()['messages']
                    self.assertEqual([m['content'] for m in messages[1:]],texts)
                    self.assertEqual(len(set(m['id'] for m in messages)),4)
                    self.assertEqual(len(snapshots),4)
                    client.post('/api/stream',json=body)
                    self.assertEqual(calls,['小婉','小研','小婉'])
                    self.assertEqual(len(client.get('/api/messages').json()['messages']),4)
            finally:store.engine.dispose()
