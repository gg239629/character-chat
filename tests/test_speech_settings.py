import json
import unittest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import test_home_settings as fixtures
import server
from backend.app.characters import speech_settings,model_defaults
from backend.app.infrastructure import storage

class SpeechSettingsTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_speech_key_is_encrypted_scoped_and_blank_preserves_key(self):
        cfg={'model':'user-tts','base_url':'https://voice.example/v1','api_key':'voice-secret','inherit_connection':False}
        result=speech_settings.save('owner',cfg)
        self.assertTrue(result['has_key']);self.assertNotIn('api_key',result)
        with Session(self.store.engine) as db:
            self.assertNotIn('voice-secret',json.dumps(db.get(model_defaults.Defaults,'owner').settings))
        speech_settings.save('owner',{**cfg,'api_key':''})
        self.assertEqual(speech_settings.read('owner')['api_key'],'voice-secret')
        self.assertFalse(speech_settings.public('other')['has_key'])
        self.assertEqual(model_defaults.read('owner'),{})
        model_defaults.save('owner',{'model':'chat','api_key':'chat-secret'})
        self.assertEqual(speech_settings.read('owner')['api_key'],'voice-secret')
        self.assertEqual(model_defaults.read('owner')['model'],'chat')

    def test_character_voices_are_empty_independent_and_clearable(self):
        with TestClient(server.app) as client:
            for role in ['wan','yan','liu']:
                self.assertEqual(client.get('/api/settings',headers={'X-Chat-Channel':role}).json()['base_voice'],'')
            for role,voice in [('wan','my-voice-a'),('yan','my-voice-b')]:
                response=client.post('/api/settings',headers={'X-Chat-Channel':role},json={'base_voice':voice})
                self.assertEqual(response.status_code,200);self.assertEqual(response.json()['base_voice'],voice)
            self.assertEqual(client.get('/api/settings',headers={'X-Chat-Channel':'wan'}).json()['base_voice'],'my-voice-a')
            self.assertEqual(client.post('/api/settings',json={'base_voice':'group-voice'}).status_code,422)
            self.assertEqual(client.post('/api/settings',headers={'X-Chat-Channel':'wan'},json={'base_voice':''}).json()['base_voice'],'')

    def test_speech_api_saves_and_rejects_invalid_url(self):
        with TestClient(server.app) as client:
            self.assertEqual(client.get('/api/speech-settings').json()['model'],'')
            self.assertEqual(client.put('/api/speech-settings',json={'model':'tts','base_url':'file:///bad'}).status_code,422)
            response=client.put('/api/speech-settings',json={'model':'my-tts','inherit_connection':True})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()['model'],'my-tts')

    def test_speech_switch_and_model_only_connection_check(self):
        from unittest.mock import AsyncMock,MagicMock,patch
        from types import SimpleNamespace as S
        fake=MagicMock();fake.__aenter__=AsyncMock(return_value=fake);fake.__aexit__=AsyncMock()
        fake.models.list=AsyncMock(return_value=S(data=[S(id='draft-tts')]))
        with TestClient(server.app) as client:
            self.assertFalse(client.get('/api/speech-settings').json()['enabled'])
            saved=client.put('/api/speech-settings',json={'enabled':True,'model':'my-tts','base_url':'https://voice.example/v1','api_key':'test-key','inherit_connection':False})
            self.assertTrue(saved.json()['enabled'])
            with patch('openai.AsyncOpenAI',return_value=fake):
                checked=client.post('/api/speech-settings/test',json={'model':'draft-tts','base_url':'https://voice.example/v1','inherit_connection':False,'actor':'liu'})
            self.assertEqual(checked.status_code,200)
            self.assertEqual(checked.json()['level'],'model')
            fake.models.list.assert_awaited_once()
            self.assertEqual(client.get('/api/speech-settings').json()['model'],'my-tts')
            self.assertFalse(client.put('/api/speech-settings',json={'enabled':False,'model':'my-tts'}).json()['enabled'])

    def test_connection_test_uses_only_user_configured_character_voice(self):
        from unittest.mock import AsyncMock,MagicMock,patch
        from types import SimpleNamespace as S
        fake=MagicMock();fake.__aenter__=AsyncMock(return_value=fake);fake.__aexit__=AsyncMock()
        fake.audio.speech.create=AsyncMock(return_value=S(aread=AsyncMock(return_value=b'ID3audio'),headers={'content-type':'audio/mpeg'}))
        with TestClient(server.app) as client:
            client.post('/api/settings',headers={'X-Chat-Channel':'liu'},json={'base_voice':'my-liu-voice'})
            with patch('openai.AsyncOpenAI',return_value=fake):
                result=client.post('/api/speech-settings/test',json={'model':'tts','api_key':'test','base_url':'https://voice.example/v1','inherit_connection':False,'actor':'liu'})
            self.assertEqual(result.status_code,200);self.assertEqual(result.json()['level'],'speech')
            self.assertEqual(fake.audio.speech.create.call_args.kwargs['voice'],'my-liu-voice')
            fake.models.retrieve.assert_not_called()

    def test_mimo_uses_chat_completions_and_users_own_voice(self):
        import base64
        from unittest.mock import AsyncMock,MagicMock,patch
        from types import SimpleNamespace as S
        fake=MagicMock();fake.__aenter__=AsyncMock(return_value=fake);fake.__aexit__=AsyncMock()
        fake.chat.completions.create=AsyncMock(return_value=S(choices=[S(message=S(audio={'data':base64.b64encode(b'RIFF'+bytes(4)+b'WAVE'+b'fmt ').decode()}))]))
        with TestClient(server.app) as client:
            client.post('/api/settings',headers={'X-Chat-Channel':'liu'},json={'base_voice':'my-user-voice'})
            with patch('openai.AsyncOpenAI',return_value=fake):
                response=client.post('/api/speech-settings/test',json={'model':'mimo-v2.5-tts','api_key':'test','base_url':'https://voice.example/v1','inherit_connection':False,'actor':'liu'})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()['level'],'speech')
            args=fake.chat.completions.create.call_args.kwargs
            self.assertEqual(args['audio'],{'format':'wav','voice':'my-user-voice'})
            self.assertEqual(args['messages'][-1],{'role':'assistant','content':'你好，我在这里。'})
            fake.audio.speech.create.assert_not_called()

    def test_mimo_voice_design_uses_description_in_user_message(self):
        import base64
        from unittest.mock import AsyncMock,MagicMock,patch
        from types import SimpleNamespace as S
        fake=MagicMock();fake.__aenter__=AsyncMock(return_value=fake);fake.__aexit__=AsyncMock()
        fake.chat.completions.create=AsyncMock(return_value=S(choices=[S(message=S(audio={'data':base64.b64encode(b'RIFF'+bytes(4)+b'WAVE'+b'fmt ').decode()}))]))
        with TestClient(server.app) as client:
            client.post('/api/settings',headers={'X-Chat-Channel':'wan'},json={'base_voice':'用户自己填写的声音描述'})
            with patch('openai.AsyncOpenAI',return_value=fake):
                response=client.post('/api/speech-settings/test',json={'model':'mimo-v2.5-tts-voicedesign','api_key':'test','base_url':'https://voice.example/v1','inherit_connection':False,'actor':'wan'})
            self.assertEqual(response.status_code,200)
            args=fake.chat.completions.create.call_args.kwargs
            self.assertTrue(args['messages'][0]['content'].startswith('用户自己填写的声音描述'))
            self.assertNotIn('voice',args['audio'])

    def test_private_reply_mode_is_independent_and_defaults_to_text(self):
        with TestClient(server.app) as client:
            headers={'X-Chat-Channel':'liu'}
            self.assertEqual(client.get('/api/settings',headers=headers).json()['reply_mode'],'text')
            self.assertEqual(client.post('/api/settings',headers=headers,json={'reply_mode':'voice'}).json()['reply_mode'],'voice')
            self.assertEqual(client.get('/api/settings',headers={'X-Chat-Channel':'wan'}).json()['reply_mode'],'text')
            self.assertEqual(client.post('/api/settings',json={'reply_mode':'voice'}).status_code,422)
            self.assertEqual(client.post('/api/settings',headers=headers,json={'reply_mode':'auto'}).status_code,422)

    def test_legacy_connection_missing_switch_keeps_enabled_but_explicit_off_is_respected(self):
        from backend.app.infrastructure import secrets_store
        with Session(self.store.engine) as db:
            db.add(model_defaults.Defaults(user_id='owner',settings={'speech':secrets_store.encode_settings(
                {'model':'mimo-v2.5-tts-voicedesign','api_key':'test','inherit_connection':True})}))
            db.commit()
        self.assertTrue(speech_settings.read('owner')['enabled'])
        with TestClient(server.app) as client:
            self.assertTrue(client.get('/api/speech-settings').json()['enabled'])
            self.assertTrue(client.get('/api/settings',headers={'X-Chat-Channel':'liu'}).json()['speech_enabled'])
        speech_settings.save('owner',{'enabled':False})
        self.assertFalse(speech_settings.read('owner')['enabled'])
        self.assertFalse(speech_settings.public('unconfigured')['enabled'])
