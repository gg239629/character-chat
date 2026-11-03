import base64
import json
import time
import unittest
from types import SimpleNamespace as S
from unittest.mock import MagicMock,patch
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import server
from backend.app.characters import image_generation as images,actors
from backend.app.conversations import chat_history
from backend.app.infrastructure import storage
from backend.app.runtime import role_actions
from backend.app.world import story_reset

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=')


class ImageTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def configure(self):
        return images.save_settings('owner',dict(enabled=True,inherit_connection=True,model='gpt-image-2.5',base_url='',api_key=''))

    def provider(self):
        fake=MagicMock();fake.__enter__.return_value=fake
        fake.images.generate.return_value=S(data=[S(b64_json=base64.b64encode(PNG).decode())],usage=S(input_tokens=10,output_tokens=20))
        return fake

    def enqueue(self,request='one',world='default',channel='liu'):
        return images.enqueue('owner',world,channel,channel if channel!='group' else '',
            '发一张阳台看花的自拍','1024x1024',request)

    def test_generated_image_and_message_are_durable_and_retries_do_not_charge_twice(self):
        self.configure();job=self.enqueue();fake=self.provider()
        with patch('openai.OpenAI',return_value=fake):
            images.process(job['id']);images.process(job['id'])
        fake.images.generate.assert_called_once()
        self.assertEqual(fake.call_args,None)
        self.assertEqual(images.get('owner','default','liu',job['id'])['status'],'done')
        self.assertEqual(images.content('owner',job['id']),(PNG,'image/png'))
        with role_actions.window('owner','default','liu'):
            messages=chat_history.messages()
            self.assertEqual(len(messages),2)
            self.assertIn('[[image:'+job['id']+']]',messages[-1]['content'])
        self.assertEqual(self.enqueue()['id'],job['id'])
        reopened=storage.Store(str(self.store.engine.url))
        try:
            with Session(reopened.engine) as db:self.assertEqual(db.get(images.Job,job['id']).image,PNG)
        finally:reopened.engine.dispose()

    def test_settings_encrypt_keys_and_blank_keeps_saved_key(self):
        cfg=dict(enabled=True,inherit_connection=False,model='gpt-image-2.5',base_url='http://127.0.0.1:1234/v1',api_key='private-image-key')
        result=images.save_settings('owner',cfg)
        self.assertNotIn('private-image-key',json.dumps(result));self.assertNotIn('api_key',result)
        images.save_settings('owner',{**cfg,'api_key':''})
        self.assertEqual(images.settings('owner')['api_key'],'private-image-key')
        with Session(self.store.engine) as db:self.assertTrue(db.get(images.Settings,'owner').settings['api_key'].startswith('fernet:v1:'))
        self.assertFalse(images.settings('other')['enabled'])

    def test_content_and_job_status_are_owner_world_and_channel_scoped(self):
        self.configure();job=self.enqueue()
        with patch('openai.OpenAI',return_value=self.provider()):images.process(job['id'])
        self.store.ensure('other','other',{})
        with self.assertRaises(PermissionError):images.content('other',job['id'])
        with self.assertRaises(PermissionError):images.get('owner','default','wan',job['id'])
        self.assertEqual(images.recent('other','other','liu'),[])
        with role_actions.window('owner','default','wan'):self.assertEqual(chat_history.messages(),[])

    def test_disabled_configuration_invalid_role_and_duplicate_payload_are_rejected(self):
        with self.assertRaises(ValueError):self.enqueue()
        self.configure();self.enqueue()
        with self.assertRaises(ValueError):images.enqueue('owner','default','liu','wan','自拍','1024x1024','two')
        with self.assertRaises(ValueError):images.enqueue('owner','default','liu','liu','不同描述','1024x1024','one')
        with self.assertRaises(ValueError):images.enqueue('owner','default','liu','liu','自拍','bad-size','three')

    def test_provider_failure_has_no_automatic_paid_retry_or_secret_leak(self):
        self.configure();job=self.enqueue();fake=self.provider()
        class Denied(Exception):status_code=403
        fake.images.generate.side_effect=Denied('secret-key-in-provider-body')
        with patch('openai.OpenAI',return_value=fake):
            images.process(job['id']);images.process(job['id'])
        fake.images.generate.assert_called_once()
        state=images.get('owner','default','liu',job['id'])
        self.assertEqual(state['status'],'failed');self.assertNotIn('secret-key',state['error'])
        with self.assertRaises(PermissionError):images.content('owner',job['id'])

    def test_crash_recovery_marks_uncertain_generation_failed_without_rerunning(self):
        self.configure();job=self.enqueue()
        with Session(self.store.engine) as db:
            row=db.get(images.Job,job['id']);row.status='running';row.lease_until=time.time()-1;db.commit()
        self.assertNotIn(job['id'],images.due())
        self.assertEqual(images.get('owner','default','liu',job['id'])['status'],'failed')

    def test_visual_reference_uses_only_this_private_window(self):
        self.configure()
        with role_actions.window('owner','default','wan'):
            chat_history.append('secret',0,'user','wan-only-private-secret')
        job=self.enqueue();fake=self.provider()
        with patch('openai.OpenAI',return_value=fake):images.process(job['id'])
        prompt=fake.images.generate.call_args.kwargs['prompt']
        self.assertNotIn('wan-only-private-secret',prompt)
        self.assertIn('小柳',prompt)

    def test_reset_deletes_images_and_late_result_does_not_restore_them(self):
        self.configure();job=self.enqueue();fake=self.provider()
        def generate(**kwargs):
            story_reset.reset_story('owner','default',backup=False)
            return S(data=[S(b64_json=base64.b64encode(PNG).decode())],usage=None)
        fake.images.generate.side_effect=generate
        with patch('openai.OpenAI',return_value=fake):images.process(job['id'])
        with self.assertRaises(PermissionError):images.content('owner',job['id'])
        with role_actions.window('owner','default','liu'):self.assertEqual(chat_history.messages(),[])
        self.assertTrue(images.settings('owner')['enabled'])

    def test_backup_keeps_binary_image_before_explicit_reset(self):
        self.configure();job=self.enqueue()
        with patch('openai.OpenAI',return_value=self.provider()):images.process(job['id'])
        result=story_reset.reset_story('owner','default',self.root/'backups')
        from pathlib import Path
        backup=json.loads(Path(result['backup']).read_text(encoding='utf-8'))
        encoded=backup['tables']['chat_image_jobs'][0]['image']
        self.assertEqual(encoded['encoding'],'base64')
        self.assertEqual(base64.b64decode(encoded['data']),PNG)
        with self.assertRaises(PermissionError):images.content('owner',job['id'])

    def test_routes_generate_poll_and_render_persistent_image(self):
        self.configure()
        with TestClient(server.app) as client,patch('openai.OpenAI',return_value=self.provider()):
            headers={'X-Chat-Channel':'liu'}
            reply=client.post('/api/images',headers=headers,json={'prompt':'发张自拍','request_id':'api-one'})
            self.assertEqual(reply.status_code,202,reply.text)
            identity=reply.json()['id']
            state=client.get('/api/images/'+identity,headers=headers)
            self.assertEqual(state.json()['status'],'done')
            image=client.get('/api/images/'+identity+'/content')
            self.assertEqual(image.content,PNG);self.assertEqual(image.headers['content-type'],'image/png')
            self.assertIn('no-store',image.headers['cache-control'])
            self.assertEqual(client.get('/api/images/'+identity,headers={'X-Chat-Channel':'wan'}).status_code,404)

    def test_rejects_non_image_data_and_large_encoded_payloads(self):
        with self.assertRaises(ValueError):images.decode_image(S(b64_json=base64.b64encode(b'<svg>unsafe</svg>').decode()),'')
        with patch.object(images,'MAX_BYTES',4):
            with self.assertRaises(ValueError):images.decode_image(S(b64_json=base64.b64encode(PNG).decode()),'')

    def test_image_descriptions_do_not_move_people_or_complete_real_world_actions(self):
        records=[{'role':'user','content':'[生图请求] 小柳走进厨房煮面'},
            {'role':'assistant','speaker':'小柳','content':'小柳：（走进厨房，盛好面）面好了。\n[[image:'+'a'*64+']]'}]
        with role_actions.window('owner','default','liu'),patch('openai.OpenAI') as provider:
            server.update_scene_state(records)
            provider.assert_not_called()
        state=self.store.read('owner','default')
        self.assertNotIn('liu',state.get('role_positions',{}))
        self.assertNotIn('household_meal',state)

    def test_dismiss_failed_job_persists_and_checks_owner_world_channel_and_status(self):
        self.configure();job=self.enqueue()
        with self.assertRaises(ValueError):images.dismiss('owner','default','liu',job['id'])
        with Session(self.store.engine) as db:
            db.get(images.Job,job['id']).status='failed';db.commit()
        with self.assertRaises(PermissionError):images.dismiss('owner','default','wan',job['id'])
        self.store.ensure('other','other',{})
        with self.assertRaises(PermissionError):images.dismiss('other','other','liu',job['id'])
        with self.assertRaises(PermissionError):images.dismiss('owner','wrong-world','liu',job['id'])
        with TestClient(server.app) as client:
            headers={'X-Chat-Channel':'liu'}
            self.assertEqual(client.post('/api/images/'+job['id']+'/dismiss',headers={'X-Chat-Channel':'wan'}).status_code,404)
            self.assertEqual(client.post('/api/images/'+job['id']+'/dismiss',headers=headers).status_code,200)
            self.assertEqual(client.post('/api/images/'+job['id']+'/dismiss',headers=headers).status_code,200)
            self.assertEqual(client.get('/api/images',headers=headers).json(),[])
        reopened=storage.Store(str(self.store.engine.url))
        try:
            with Session(reopened.engine) as db:self.assertEqual(db.get(images.Job,job['id']).status,'dismissed')
        finally:reopened.engine.dispose()
        with patch('openai.OpenAI') as provider:images.process(job['id']);provider.assert_not_called()
