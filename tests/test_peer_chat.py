import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from backend.app.infrastructure import storage
from backend.app.characters import actors
import server
from backend.app.conversations import chat_history
from backend.app.conversations import peer_chat as peer
from backend.app.runtime import role_actions as actions
from backend.app.memory import role_memory


class PeerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'peer.db').as_posix())
        self.store.ensure('owner','default',{'scene_state':{},'recent_log':[]})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict('os.environ',{
            'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),
            patch.object(server,'load_config',return_value={'api_key':'fake','model':'fake','base_url':'','temperature':.2})]
        for p in self.patches:p.start()
        for actor in actors.ROLES:
            with actors.role_scope('owner','default',actor):pass

    def tearDown(self):
        from backend.app.memory import memory_autosave
        memory_autosave.flush_due(force=True)
        for p in reversed(self.patches):p.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def job(self,kind,actor=None):
        with Session(self.store.engine) as db:
            query=select(peer.Job).where(peer.Job.kind==kind)
            if actor:query=query.where(peer.Job.actor==actor)
            return db.scalar(query.order_by(peer.Job.due_at)).id

    def test_send_idempotent_and_participant_memory_only(self):
        for _ in range(2):peer.send('owner','default','yan','liu','面已经做好，来吃吧。','meal')
        self.assertEqual(len(peer.history('owner','default','liu','yan')),1)
        self.assertIn('面已经做好',json.dumps(peer.recall('owner','default','liu','面做好了吗'),ensure_ascii=False))
        self.assertEqual(peer.recall('owner','default','wan','面做好了吗'),[])
        with self.assertRaises(PermissionError):peer.history('intruder','default','liu','yan')
        self.store.ensure('owner','other',{})
        self.assertEqual(peer.history('owner','other','liu','yan'),[])
        with Session(self.store.engine) as db:
            self.assertEqual(len(list(db.scalars(select(peer.Job)))),1)

    def test_no_user_api_unread_export_or_notifications(self):
        action=actions.insert_action('owner','default','yan','liu','autonomous','hidden',{'decision':'send','message':'hidden-private-marker'})
        result=asyncio.run(actions.deliver(action['id']))
        self.assertEqual(result['status'],'sent')
        with TestClient(server.app) as client:
            for channel in ('group',*actors.ROLES):
                headers={'X-Chat-Channel':channel}
                for path in ('/api/messages','/api/memory/export','/api/actions','/api/info','/api/settings','/api/conversations'):
                    response=client.get(path,headers=headers)
                    self.assertEqual(response.status_code,200)
                    self.assertNotIn('hidden-private-marker',response.text)
                self.assertEqual(client.get('/api/messages',headers=headers).json()['messages'],[])
            # Arbitrary child IDs must not be reinterpreted as public root stories.
            for child in (actors.role_id('default','liu'),peer.key('pair','yan','liu')):
                self.assertEqual(client.get('/api/messages',headers={'X-Conversation-ID':child}).status_code,404)
            self.assertEqual(client.get('/api/messages',headers={'X-Chat-Channel':'peer:liu-yan'}).status_code,400)
        from backend.app.conversations import push_notifications
        with Session(self.store.engine) as db:
            self.assertEqual(list(db.scalars(select(push_notifications.PushJob))),[])

    def test_manual_initiative_check_does_not_return_hidden_action_body(self):
        result={'id':'hidden','status':'sent','target':'yan','detail':{'message':'hidden-body-marker'}}
        with TestClient(server.app) as client,patch.object(actions,'autonomous',new=AsyncMock(return_value=result)):
            response=client.post('/api/actions/check',headers={'X-Chat-Channel':'liu'})
            self.assertEqual(response.status_code,200)
            self.assertIsNone(response.json()['action'])
            self.assertNotIn('hidden-body-marker',response.text)

    def test_inquiry_response_followup_real_order_and_retry(self):
        task=peer.enqueue_inquiry('owner','default','liu','liu','user-question','小研面做好了吗','我不清楚，我问问她。')
        self.assertEqual(task,peer.enqueue_inquiry('owner','default','liu','liu','user-question','小研面做好了吗','我不清楚。'))
        decisions=AsyncMock(side_effect=[
            {'decision':'send','target':'yan','message':'面做好了吗？'},
            {'decision':'send','message':'做好了，来吃吧。'},
            {'decision':'send','message':'我问到小研了，她说面做好了。'}])
        with patch.object(actions,'ask_json',new=decisions):
            asyncio.run(peer.process(task));asyncio.run(peer.process(task))
            messages=peer.history('owner','default','liu','yan')
            self.assertEqual(len(messages),1)
            follow=self.job('followup')
            with Session(self.store.engine) as db:
                db.execute(update(peer.Job).where(peer.Job.id==follow).values(due_at=0));db.commit()
            asyncio.run(peer.process(follow))  # No answer yet, no model call or invented followup.
            self.assertEqual(decisions.await_count,1)
            with actions.window('owner','default','liu'):self.assertEqual(chat_history.messages(),[])
            asyncio.run(peer.process(self.job('reply','yan')))
            with Session(self.store.engine) as db:
                db.execute(update(peer.Job).where(peer.Job.id==follow).values(due_at=0));db.commit()
            asyncio.run(peer.process(follow));asyncio.run(peer.process(follow))
            self.assertEqual(decisions.await_count,3)
        messages=peer.history('owner','default','liu','yan')
        self.assertEqual(messages[1]['reply_to'],messages[0]['id'])
        with actions.window('owner','default','liu'):
            self.assertEqual(len(chat_history.messages()),1)
            self.assertIn('做好',chat_history.messages()[0]['content'])
        with actions.window('owner','default','yan'):self.assertEqual(chat_history.messages(),[])

    def test_no_question_when_confident_or_actor_declines(self):
        self.assertIsNone(peer.enqueue_inquiry('owner','default','liu','liu','certain','面呢','面已经做好了。'))
        task=peer.enqueue_inquiry('owner','default','liu','liu','uncertain','她在哪里','我不知道。')
        with patch.object(actions,'ask_json',new=AsyncMock(return_value={'decision':'none'})):
            asyncio.run(peer.process(task))
        self.assertEqual(peer.history('owner','default','liu','yan'),[])

    def test_refusal_silence_delay_and_execution_depth(self):
        peer.send('owner','default','yan','liu','忙吗','root')
        task=self.job('reply','liu')
        with patch.object(actions,'ask_json',new=AsyncMock(return_value={'decision':'later','delay_minutes':3})):
            asyncio.run(peer.process(task))
        with Session(self.store.engine) as db:
            row=db.get(peer.Job,task);self.assertEqual(row.status,'pending')
            self.assertGreater(row.due_at,time.time()+100)
            row.due_at=0;db.commit()
        with patch.object(actions,'ask_json',new=AsyncMock(return_value={'decision':'none'})):
            asyncio.run(peer.process(task))
        self.assertEqual(len(peer.history('owner','default','liu','yan')),1)
        peer.send('owner','default','liu','yan','嗯','r1',reply_to='root')
        peer.send('owner','default','yan','liu','好','r2',reply_to='r1')
        peer.send('owner','default','liu','yan','知道了','r3',reply_to='r2')
        with Session(self.store.engine) as db:
            self.assertIsNone(db.get(peer.Job,peer.key('reply','r3')))
        with self.assertRaises(PermissionError):peer.send('owner','default','wan','yan','冒用回复','bad',reply_to='root')

    def test_own_peer_experience_in_normal_actor_payload(self):
        peer.send('owner','default','yan','liu','明天去植物园看兰花。','orchid')
        with actions.window('owner','default','liu'):
            _,payload=server.actor_payload('liu',[{'role':'user','content':'你们说的兰花计划呢'}],False)
            text=json.dumps(payload,ensure_ascii=False)
            self.assertIn('植物园',text);self.assertIn('peer:liu-yan',text)
        with actions.window('owner','default','wan'):
            _,payload=server.actor_payload('wan',[{'role':'user','content':'兰花计划呢'}],False)
            self.assertNotIn('植物园',json.dumps(payload,ensure_ascii=False))

    def test_status_bar_truth_does_not_give_everyone_omniscience(self):
        self.store.mutate('owner','default',lambda d:d.update(scene_state={
            '小柳':'公园·散步','小研':'密室·藏礼物','上一段停在':'只有小研知道的礼物秘密'}))
        with actions.window('owner','default','liu'):
            _,payload=server.actor_payload('liu',[{'role':'user','content':'小研在哪'}],False)
            text=json.dumps(payload,ensure_ascii=False)
            self.assertIn('公园·散步',text)
            self.assertNotIn('密室·藏礼物',text)
            self.assertNotIn('只有小研知道的礼物秘密',text)

    def test_archive_provenance_permissions_and_dedup(self):
        now=time.time()
        with Session(self.store.engine) as db:
            for i in range(150):
                db.add(peer.Message(id=f'm{i:03}',user_id='owner',world_id='default',pair='liu-yan',sender='yan',recipient='liu',
                    content=f'小研：兰花计划第{i}条',created_at=now+i,reply_to='',event_id='orchid',depth=0))
            db.commit()
        with patch.object(actions,'ask_json',new=AsyncMock(return_value={'summary':'小研在角色私聊中告诉小柳：约定去植物园看兰花，具体时间待定。'})) as summarize:
            asyncio.run(peer.archive_pair('owner','default','liu','yan'))
            asyncio.run(peer.archive_pair('owner','default','liu','yan'))
            self.assertEqual(summarize.await_count,1)
        with Session(self.store.engine) as db:
            archive=db.scalar(select(peer.Archive));self.assertEqual(len(archive.source_ids),50)
            self.assertEqual(archive.pair,'liu-yan')
        own=peer.recall('owner','default','liu','植物园兰花计划',5000)
        self.assertTrue(any(e.get('kind')=='peer_archive' for e in own))
        self.assertEqual(peer.recall('owner','default','wan','植物园兰花计划',5000),[])

    def test_reset_clears_hidden_history_jobs_and_archives_with_backup(self):
        from backend.app.world import story_reset
        peer.send('owner','default','yan','liu','旧信息','before-reset')
        result=story_reset.reset_story('owner','default',Path(self.tmp.name)/'backup')
        backup=json.loads(Path(result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(len(backup['tables']['chat_peer_messages']),1)
        self.assertEqual(peer.history('owner','default','liu','yan'),[])
        with Session(self.store.engine) as db:self.assertEqual(list(db.scalars(select(peer.Job))),[])

    def test_legacy_peer_messages_move_without_replaying_or_losing_user_history(self):
        from backend.app.memory import memory_autosave
        action=actions.insert_action('owner','default','yan','liu','autonomous','legacy',
            {'decision':'send','message':'小研：旧的角色私信'})
        with actions.window('owner','default','liu'):
            chat_history.bootstrap()
            chat_history.append('user-original',0,'user','保留用户自己的记录')
            chat_history.append('old-peer',0,'assistant','小研：旧的角色私信','小研',True,
                {'source':'role_action','action_id':action['id']})
            mid=chat_history.messages()[-1]['id']
            memory_autosave.flush(*storage.scope.get(),force=True)
        actions.finish(action['id'],'sent',{'message_id':mid})
        peer.migrate_legacy();peer.migrate_legacy()
        self.assertEqual(len(peer.history('owner','default','liu','yan')),1)
        with Session(self.store.engine) as db:self.assertEqual(list(db.scalars(select(peer.Job))),[])
        with actions.window('owner','default','liu'):
            self.assertEqual([m['content'] for m in chat_history.messages()],['保留用户自己的记录'])
            self.assertNotIn('旧的角色私信',json.dumps(storage.memory_pack(),ensure_ascii=False))


if __name__=='__main__':unittest.main()
