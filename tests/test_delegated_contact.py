import asyncio
import json
import unittest
from unittest.mock import patch,AsyncMock
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
import test_home_settings as fixtures
import actors
import chat_history
import role_actions as actions
import peer_chat
import server


class DelegationTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_delegated_group_message_really_appears_in_group(self):
        with TestClient(server.app) as client:
            with patch.object(actions,'ask_json',new=AsyncMock(return_value={'decision':'send','message':'请在群里发1。'})):
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={
                    'request_id':'group-delegate','messages':[{'role':'user','content':'让小柳在群里发个1'}]})
                self.assertNotIn('"error":',response.text)
            messages=peer_chat.history('owner','default','yan','liu')
            self.assertEqual(len(messages),1)
            with patch.object(actions,'ask_json',new=AsyncMock(return_value={'decision':'send','message':'我来发。',
                'user_action':{'decision':'send','message':'1'}})):
                asyncio.run(peer_chat.process(peer_chat.key('reply',messages[0]['id'])))
            group=client.get('/api/messages').json()['messages']
            self.assertEqual([m['content'] for m in group],['小柳：1'])
            self.assertEqual(client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages'],[])
    def test_colloquial_request_and_pronoun_followups_route_to_correct_person(self):
        messages=[]
        for text in ['你让小柳给我发个消息我有点找不到聊天记录了','你跟她说一下让她自己发',
            '让她发个1就行','说了吗','快去','让小柳发','你跟他说一声让她给我发个消息怎么这么费劲','快点','我怎么没收到']:
            messages.append({'role':'user','content':text})
            self.assertEqual(actions.resolve_target(messages,'yan','yan'),'liu',text)
        self.assertIsNone(actions.resolve_target([{'role':'user','content':'小研刚刚是不是让你给我发消息了'}],'liu','liu'))
        self.assertEqual(actions.group_request_roles('小研，你让小柳私信给我发个1'),['yan'])
        self.assertEqual(actions.cross_target('小研，你让小柳私信给我发个1','yan','group'),'liu')

    def test_receiving_role_can_really_contact_user_with_switch_off_and_no_duplicates(self):
        with TestClient(server.app) as client:
            client.put('/api/home/settings',json={'initiative_enabled':False})
            plan={'decision':'send','message':'用户让你给用户发个1，你自己决定。',
                'recipient_task':{'type':'contact_user','message_hint':'1'}}
            with patch.object(actions,'ask_json',new=AsyncMock(return_value=plan)):
                response=client.post('/api/stream',headers={'X-Chat-Channel':'yan'},json={'request_id':'contact',
                    'messages':[{'role':'user','content':'你让小柳给我发个消息，发个1就行'}]})
                self.assertNotIn('"error":',response.text)
            messages=peer_chat.history('owner','default','yan','liu')
            self.assertEqual(len(messages),1)
            with Session(self.store.engine) as db:
                action=db.scalar(select(actions.Action).where(actions.Action.kind=='requested'))
                self.assertEqual(action.detail['recipient_task']['type'],'contact_user')
            answer={'decision':'send','message':'我看看。','user_action':{'decision':'send','message':'1'}}
            job=peer_chat.key('reply',messages[0]['id'])
            with patch.object(actions,'ask_json',new=AsyncMock(return_value=answer)):
                asyncio.run(peer_chat.process(job));asyncio.run(peer_chat.process(job))
            visible=client.get('/api/messages',headers={'X-Chat-Channel':'liu'}).json()['messages']
            self.assertEqual([m['content'] for m in visible],['小柳：1'])
            self.assertEqual(visible[0]['speaker'],'小柳')
            status=actions.received_contact_status('owner','default','liu','小研刚刚是不是让你给我发消息了')
            self.assertIn('小研确实通过私信',status)
            self.assertIn('已经给你发过',status)
            self.assertIsNone(actions.received_contact_status('owner','default','liu','你是不是收到快递了'))
            self.assertIsNone(actions.received_contact_status('owner','default','wan','小研刚刚是不是让你给我发消息了'))
            own=peer_chat.recall('owner','default','liu','小研让你发消息了吗',2400)
            self.assertIn('用户让你',json.dumps(own,ensure_ascii=False))
            self.assertEqual(client.get('/api/messages').json()['messages'],[])
            self.assertEqual(client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages'],[])

    def test_contact_refusal_and_non_authorized_peer_claim_never_send_user_message(self):
        # Autonomous messages cannot manufacture authority by saying "the user asked".
        for source,kind,authorized in [('untrusted','autonomous',False),('refuse','requested',True)]:
            decision={'decision':'send','message':'用户让你发1','_recipient_task':{'type':'contact_user','message_hint':'1'}}
            action=actions.insert_action('owner','default','yan','liu',kind,source,decision)
            result=asyncio.run(actions.deliver(action['id']))
            answer={'decision':'send','message':'不发。','user_action':{'decision':'refuse' if authorized else 'send','message':'1'}}
            with patch.object(actions,'ask_json',new=AsyncMock(return_value=answer)):
                asyncio.run(peer_chat.process(peer_chat.key('reply',result['detail']['message_id'])))
        with actions.window('owner','default','liu'):self.assertEqual(chat_history.messages(),[])

    def test_delivery_status_reports_pending_receipt_without_inventing_refusal(self):
        decision={'decision':'send','message':'用户让你给用户发1','_recipient_task':{'type':'contact_user','message_hint':'1'}}
        action=actions.insert_action('owner','default','yan','liu','requested','status',decision)
        asyncio.run(actions.deliver(action['id']))
        with actions.window('owner','default','yan'),patch.object(actions,'ask_json',new=AsyncMock()) as ask:
            result=asyncio.run(actions.requested('yan','liu',[{'role':'user','content':'我怎么没收到'}],'status-query'))
            self.assertIn('不能认定她拒绝',result['detail']['delivery_report'])
            ask.assert_not_awaited()
