import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
import chat_history
import push_notifications as push
import server
import storage


class PushTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import AsyncMock
        import actors
        self.coordinator_patch=patch.object(server.group_coordinator,'plan',new=AsyncMock(side_effect=lambda messages,configs:[role for role in actors.candidates(messages) if not any(m.get('role')=='assistant' and m.get('content','').startswith(actors.ROLES[role]+'：') for m in server.recent_rounds(messages,1))]))
        self.coordinator_patch.start()
        self.addCleanup(self.coordinator_patch.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'chat.db').as_posix())
        self.store.ensure('owner','default',{})
        key=ec.generate_private_key(ec.SECP256R1())
        raw=push.b64(key.private_numbers().private_value.to_bytes(32,'big'))
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict('os.environ',{
            'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),
            'VAPID_PRIVATE_KEY':raw})]
        for item in self.patches:item.start()
        self.subscription={'endpoint':'https://fcm.googleapis.com/fcm/send/test-device',
            'keys':{'p256dh':push.b64(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)),
                    'auth':push.b64(b'x'*16)}}

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def due(self):
        with Session(self.store.engine) as db:
            db.execute(update(push.PushJob).values(due_at=time.time()-10));db.commit()

    def test_subscriptions_encrypted_idempotent_owner_and_ssrf(self):
        with TestClient(server.app) as client:
            pair=client.get('/api/push/config').json()
            self.assertEqual(len(pair['public_key']),87)
            self.assertEqual(client.post('/api/push/subscribe',json=self.subscription).status_code,200)
            self.assertEqual(client.post('/api/push/subscribe',json=self.subscription).status_code,200)
            for endpoint in ('http://127.0.0.1:1234/x','https://localhost/test','https://fcm.googleapis.com.evil.test/test','https://name:pass@fcm.googleapis.com/test'):
                bad={**self.subscription,'endpoint':endpoint}
                self.assertEqual(client.post('/api/push/subscribe',json=bad).status_code,422)
        with Session(self.store.engine) as db:
            rows=list(db.scalars(select(push.Subscription)))
            self.assertEqual(len(rows),1)
            self.assertNotIn('fcm.googleapis.com',rows[0].encrypted)
        with self.assertRaises(PermissionError):
            push.subscribe('other',self.subscription)

    def test_push_skips_read_and_cleared_messages(self):
        push.subscribe('owner',self.subscription)
        chat_history.bootstrap()
        chat_history.append('read',0,'assistant','小研：秘密原文','小研')
        message=chat_history.messages()[-1]
        push.enqueue('owner','default','group',message['id']);self.due()
        chat_history.mark_read(message['id'])
        with patch('pywebpush.webpush') as send:
            push.flush();send.assert_not_called()
        chat_history.append('clear',0,'assistant','小研：另一条','小研')
        message=chat_history.messages()[-1]
        push.enqueue('owner','default','group',message['id']);self.due();chat_history.clear_display()
        with patch('pywebpush.webpush') as send:
            push.flush();send.assert_not_called()

    def test_unread_push_generic_payload_and_no_repeat_after_success(self):
        push.subscribe('owner',self.subscription)
        chat_history.bootstrap();chat_history.append('push',0,'assistant','小研：秘密原文','小研')
        message=chat_history.messages()[-1]
        push.enqueue('owner','default','group',message['id']);push.enqueue('owner','default','group',message['id']);self.due()
        with patch('pywebpush.webpush',return_value=Mock(status_code=201)) as send:
            push.flush();push.flush()
            self.assertEqual(send.call_count,1)
            payload=json.loads(send.call_args.kwargs['data'])
            self.assertNotIn('秘密原文',send.call_args.kwargs['data'])
            self.assertEqual(payload['message_id'],message['id'])
            self.assertIn('channel=group',payload['url'])
        push.unsubscribe('owner',self.subscription['endpoint'])
        with Session(self.store.engine) as db:
            self.assertEqual(list(db.scalars(select(push.Subscription))),[])

    def test_invalid_key_or_subscription_never_sends_network(self):
        with self.assertRaises(ValueError):
            push.subscribe('owner',{'endpoint':self.subscription['endpoint'],'keys':{'p256dh':'bad','auth':'bad'}})


if __name__=='__main__':
    unittest.main()
