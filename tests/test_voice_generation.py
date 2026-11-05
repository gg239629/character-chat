import asyncio
import json
import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock,patch
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import server
from backend.app.characters import speech_settings,voice_generation as voices
from backend.app.conversations import chat_inbox,chat_history
from backend.app.runtime import role_actions
from backend.app.infrastructure import storage

WAV=b'RIFF'+bytes(4)+b'WAVEfmt '

class VoiceTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def configure(self):
        speech_settings.save('owner',{'enabled':True,'model':'mimo-v2.5-tts','api_key':'test','base_url':'https://example.test/v1','inherit_connection':False})
        with role_actions.window('owner','default','liu'):
            storage.write_document('base_voice','user-liu-voice');storage.write_document('reply_mode','voice')

    def enqueue(self,request='one'):
        return voices.enqueue(request,'liu',{0:{'name':'send_voice','arguments':json.dumps({'text':'小柳：（擦手）等我一下。（轻笑）','style':'亲切，稍慢，短暂停顿'})}},'发条语音')

    def test_tool_saves_message_before_audio_and_deduplicates_generation(self):
        self.configure();captured=[]
        async def create(**kwargs):
            captured.append(kwargs)
            self.assertFalse(any(t['function']['name']=='send_voice' for t in kwargs['tools']))
            async def chunks():
                yield S(choices=[S(delta=S(content='小柳：（擦手）等我一下。'))])
            return chunks()
        model=S(chat=S(completions=S(create=create)),close=AsyncMock())
        with TestClient(server.app) as client:
            headers={'X-Chat-Channel':'liu'}
            accepted=client.post('/api/send',headers=headers,json={'request_id':'voice-turn','messages':[{'role':'user','content':'想听你的声音'}]}).json()
            with patch.object(server,'make_client',return_value=model),patch.object(voices,'start') as start:
                asyncio.run(chat_inbox.process(accepted['id']))
                self.assertEqual(start.call_count,1)
            messages=client.get('/api/messages',headers=headers).json()['messages']
            self.assertEqual(len(messages),2);self.assertIn('[[voice:',messages[-1]['content'])
            self.assertIn('擦手',messages[-1]['content'])
            identity=messages[-1]['content'].split('[[voice:')[1].split(']]')[0]
            self.assertEqual(client.get('/api/voices/'+identity,headers=headers).json()['status'],'pending')
            self.assertEqual(client.get('/api/voices/'+identity,headers={'X-Chat-Channel':'yan'}).status_code,404)
            with patch.object(speech_settings,'synthesize',new=AsyncMock(return_value=(WAV,'audio/wav'))) as synth:
                voices.process(identity);voices.process(identity)
            synth.assert_awaited_once()
            self.assertEqual(synth.call_args.args[1:3],('user-liu-voice','等我一下。'))
            self.assertEqual(client.get('/api/voices/'+identity+'/content').content,WAV)
            with self.assertRaises(PermissionError):voices.content('other',identity)

    def test_disabled_or_missing_voice_never_offers_tool_and_group_needs_request(self):
        self.configure()
        self.assertTrue(voices.available('owner','default','liu','liu','晚安'))
        self.assertFalse(voices.available('owner','default','group','liu','晚安'))
        self.assertTrue(voices.available('owner','default','group','liu','发一条语音'))
        self.assertFalse(voices.available('owner','default','group','liu','不要发语音'))
        self.assertFalse(voices.available('owner','default','group','liu','你能发语音吗？'))
        self.assertFalse(voices.available('owner','default','yan','yan','发条语音'))
        speech_settings.save('owner',{'enabled':False})
        self.assertFalse(voices.available('owner','default','liu','liu','发条语音'))

    def test_failed_audio_keeps_transcript_and_draft_is_not_charged(self):
        self.configure()
        with role_actions.window('owner','default','liu'):
            job=self.enqueue()
            self.assertEqual(job['status'],'draft');self.assertNotIn(job['id'],voices.due())
            text=voices.attach(job,'小柳：等我一下。')
            chat_history.append('one',1,'assistant',text,'小柳')
            self.assertEqual(self.enqueue()['id'],job['id'])
        with patch.object(speech_settings,'synthesize',new=AsyncMock(side_effect=RuntimeError('provider failed'))):voices.process(job['id'])
        self.assertEqual(voices.get('owner','default','liu',job['id'])['status'],'failed')
        with role_actions.window('owner','default','liu'):self.assertIn('等我一下。',chat_history.messages()[-1]['content'])

    def test_balanced_parentheses_are_not_spoken(self):
        self.assertEqual(voices.spoken('小柳：（低头（擦手））等我一下。(笑)'),'等我一下。')

    def test_text_mode_keeps_reply_as_text_even_when_global_voice_is_enabled(self):
        self.configure()
        with role_actions.window('owner','default','liu'):storage.write_document('reply_mode','text')
        async def create(**kwargs):
            self.assertFalse(any(t['function']['name']=='send_voice' for t in kwargs['tools']))
            async def chunks():yield S(choices=[S(delta=S(content='小柳：（擦手）等我一下。'))])
            return chunks()
        model=S(chat=S(completions=S(create=create)),close=AsyncMock())
        with TestClient(server.app) as client:
            headers={'X-Chat-Channel':'liu'}
            accepted=client.post('/api/send',headers=headers,json={'request_id':'text-mode','messages':[{'role':'user','content':'你好'}]}).json()
            with patch.object(server,'make_client',return_value=model):asyncio.run(chat_inbox.process(accepted['id']))
            message=client.get('/api/messages',headers=headers).json()['messages'][-1]
            self.assertNotIn('[[voice:',message['content'])
            self.assertIn('擦手',message['content'])
