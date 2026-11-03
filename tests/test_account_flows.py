import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from backend.app.accounts import accounts
from backend.app.characters import model_defaults
from backend.app.conversations import push_notifications
import server
from backend.app.infrastructure import storage


class AccountFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'backend.app.accounts.accounts.db').as_posix())
        self.store.ensure('owner','default',{'scene_state':{'时间':'原有进度'},'recent_log':[]})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict(os.environ,{
            'ENABLE_USER_ACCOUNTS':'true','REGISTRATION_MODE':'invite','REGISTRATION_INVITE_CODE':'test-invite-code-12345',
            'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'})]
        for p in self.patches:p.start()
        self.client=TestClient(server.app);self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for p in reversed(self.patches):p.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def register(self,name='alice',code='test-invite-code-12345'):
        return self.client.post('/api/auth/register',json={'username':name,'password':'example-password-123','invite_code':code})

    def login(self,name='alice',password='example-password-123'):
        return self.client.post('/api/auth/login',json={'username':name,'password':password})

    def test_invitation_and_modes(self):
        self.assertEqual(self.register(code='wrong').status_code,422)
        self.assertEqual(self.register().status_code,200)
        self.assertEqual(self.register().status_code,422)
        with patch.dict(os.environ,{'REGISTRATION_MODE':'closed'}):
            self.assertEqual(self.register('bob').status_code,422)
        with patch.dict(os.environ,{'REGISTRATION_MODE':'open'}):
            self.assertEqual(self.register('bob',code='').status_code,200)

    def test_owner_binding_keeps_existing_data_and_not_first_registrant(self):
        self.register();self.login()
        self.assertEqual(self.client.get('/api/messages',headers={'X-Conversation-ID':'default'}).status_code,200)
        user=accounts.identify(self.client.cookies.get('chat_session'))
        self.assertNotEqual(user,'owner')
        self.assertIn('realtime_clock',self.store.read(user,accounts.root_id(user)))
        self.assertFalse(self.store.read(user,accounts.root_id(user))['initiative_enabled'])
        accounts.create_owner('admin','admin-password-123')
        self.login('admin','admin-password-123')
        self.assertEqual(accounts.identify(self.client.cookies.get('chat_session')),'owner')
        self.assertEqual(self.store.read('owner','default')['scene_state']['时间'],'原有进度')
        with self.assertRaises(ValueError):accounts.create_owner('another','another-password-123')

    def test_password_change_revokes_all_sessions_and_subscriptions(self):
        self.register();self.login();old=self.client.cookies.get('chat_session');user=accounts.identify(old)
        second=accounts.login('alice','example-password-123')
        with Session(self.store.engine) as db:
            db.add(push_notifications.Subscription(id='device',user_id=user,encrypted='test'));db.commit()
        response=self.client.post('/api/auth/password',json={'current_password':'wrong','new_password':'changed-password-123'})
        self.assertEqual(response.status_code,422);self.assertEqual(accounts.identify(old),user)
        self.assertEqual(self.client.post('/api/auth/password',json={'current_password':'example-password-123','new_password':'changed-password-123'}).status_code,200)
        self.assertIsNone(accounts.identify(old));self.assertIsNone(accounts.identify(second))
        with Session(self.store.engine) as db:self.assertIsNone(db.get(push_notifications.Subscription,'device'))
        self.assertEqual(self.login(password='example-password-123').status_code,401)
        self.assertEqual(self.login(password='changed-password-123').status_code,200)

    def test_request_source_and_session_scope(self):
        payload={'username':'alice','password':'example-password-123','invite_code':'test-invite-code-12345'}
        self.assertEqual(self.client.post('/api/auth/register',json=payload,headers={'origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.post('/api/auth/register',json=payload,headers={'sec-fetch-site':'cross-site'}).status_code,403)
        self.register();self.login()
        self.assertEqual(self.client.get('/api/messages',headers={'X-Session-Scope':'old-account'}).status_code,409)
        scope=self.client.get('/api/auth/status').json()['scope']
        self.assertEqual(self.client.get('/api/messages',headers={'X-Session-Scope':scope}).status_code,200)

    def test_other_accounts_cannot_read_models_or_chat_and_expired_sessions_fail(self):
        self.register();self.login();alice=accounts.identify(self.client.cookies.get('chat_session'))
        model_defaults.save(alice,{'model':'private-model','api_key':'private-key'})
        self.register('bob');self.login('bob')
        cfg=self.client.get('/api/settings').json()
        self.assertNotIn('private-model',str(cfg));self.assertNotIn('private-key',str(cfg))
        self.assertEqual(self.client.get('/api/messages',headers={'X-Conversation-ID':accounts.root_id(alice)}).status_code,404)
        token=self.client.cookies.get('chat_session')
        with Session(self.store.engine) as db:
            db.get(accounts.LoginSession,hashlib.sha256(token.encode()).hexdigest()).expires=0;db.commit()
        self.assertEqual(self.client.get('/api/messages').status_code,401)

    def test_rate_limit_is_persistent_and_reset_revokes_sessions(self):
        self.assertTrue(accounts.allow_attempt('test',limit=2));self.assertTrue(accounts.allow_attempt('test',limit=2))
        self.assertFalse(accounts.allow_attempt('test',limit=2))
        self.register();self.login();token=self.client.cookies.get('chat_session');user=accounts.identify(token)
        accounts.change_password(user,None,'reset-password-123')
        self.assertIsNone(accounts.identify(token))
        self.assertEqual(self.login(password='reset-password-123').status_code,200)

    def test_logout_removes_only_current_account_device(self):
        self.register();self.login();token=self.client.cookies.get('chat_session');user=accounts.identify(token)
        endpoint='https://push.example/device';key=hashlib.sha256(endpoint.encode()).hexdigest()
        with Session(self.store.engine) as db:
            db.add(push_notifications.Subscription(id=key,user_id=user,encrypted='test'));db.add(push_notifications.Subscription(id='other',user_id=user,encrypted='test'));db.commit()
        self.assertEqual(self.client.post('/api/auth/logout',json={'endpoint':endpoint}).status_code,200)
        self.assertIsNone(accounts.identify(token))
        with Session(self.store.engine) as db:
            self.assertIsNone(db.get(push_notifications.Subscription,key));self.assertIsNotNone(db.get(push_notifications.Subscription,'other'))

    def test_login_replaces_expired_previous_device_without_transferring_it(self):
        self.register();self.login();old=self.client.cookies.get('chat_session');user=accounts.identify(old)
        endpoint='https://push.example/expired';key=hashlib.sha256(endpoint.encode()).hexdigest()
        with Session(self.store.engine) as db:
            db.get(accounts.LoginSession,hashlib.sha256(old.encode()).hexdigest()).expires=0
            db.add(push_notifications.Subscription(id=key,user_id=user,encrypted='test'));db.commit()
        self.register('bob')
        response=self.client.post('/api/auth/login',json={'username':'bob','password':'example-password-123','endpoint':endpoint})
        self.assertEqual(response.status_code,200)
        self.assertNotEqual(accounts.identify(self.client.cookies.get('chat_session')),user)
        with Session(self.store.engine) as db:self.assertIsNone(db.get(push_notifications.Subscription,key))

    def test_database_policy_requires_owner_and_environment_can_override(self):
        import copy
        previous=copy.deepcopy(accounts._runtime_policy)
        try:
            with self.assertRaises(ValueError):accounts.configure_policy('policy-invite-code-12345')
            accounts.create_owner('admin','admin-password-123')
            accounts.configure_policy('policy-invite-code-12345')
            accounts.load_policy()
            with patch.dict(os.environ,{},clear=True):
                self.assertTrue(accounts.enabled());self.assertEqual(accounts.registration_mode(),'invite')
                accounts.check_invite('policy-invite-code-12345')
                with self.assertRaises(ValueError):accounts.check_invite('wrong-code')
            with patch.dict(os.environ,{'ENABLE_USER_ACCOUNTS':'false'}):self.assertFalse(accounts.enabled())
        finally:accounts._runtime_policy=previous
