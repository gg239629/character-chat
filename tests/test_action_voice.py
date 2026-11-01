import asyncio
import unittest
from unittest.mock import patch,AsyncMock
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import server
import role_actions
import action_voice
import actors


class ActionVoiceTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_sent_response_preserves_character_voice_without_extra_model_call(self):
        decision={'decision':'send','message':'小研，给他发个1呗。','reply':'我帮你说一声。','sent_reply':'催到我这儿来了？行，跟她说过了。',
            'recipient_task':{'type':'contact_user','message_hint':'1'}}
        with TestClient(server.app) as client,patch.object(role_actions,'ask_json',new=AsyncMock(return_value=decision)) as ask:
            response=client.post('/api/stream',headers={'X-Chat-Channel':'wan'},json={
                'request_id':'voice','messages':[{'role':'user','content':'让小研给我发个1'}]})
            self.assertNotIn('"error":',response.text)
            messages=client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages']
            self.assertEqual(messages[-1]['content'],'小婉：催到我这儿来了？行，跟她说过了。')
            self.assertNotIn('已发送到',messages[-1]['content']);ask.assert_awaited_once()

    def test_followup_uses_verified_fact_personality_and_can_skip(self):
        cfg={'model':'fake','api_key':'fake'}
        with actors.role_scope('owner','default','wan'),patch.object(role_actions,'ask_json',new=AsyncMock(return_value={
            'decision':'send','message':'她发啦，去看看。'})) as ask:
            text=asyncio.run(action_voice.phrase('wan',cfg,'小研实际发到用户私聊。',followup=True))
            self.assertEqual(text,'小婉：她发啦，去看看。')
            self.assertIn('小研实际发到用户私聊',str(ask.call_args))
        with patch.object(role_actions,'ask_json',new=AsyncMock(return_value={'decision':'none'})):
            self.assertIsNone(asyncio.run(action_voice.phrase('wan',cfg,'已发送',followup=True)))
