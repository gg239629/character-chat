"""Cross-module invariants: one source, durable history, scoped knowledge."""
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import MagicMock,patch
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from backend.app.infrastructure import storage
from backend.app.world import state_events as events,continuity,realtime_world,actor_context,story_reset
from backend.app.conversations import peer_chat
import server  # Bind the production service facade for the integration path.


class StateEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'test.db').as_posix())
        self.store.ensure('owner','world',{})
        self.patch=patch.object(storage,'_STORE',self.store);self.patch.start()

    def tearDown(self):
        self.patch.stop();self.store.engine.dispose();self.tmp.cleanup()

    def record(self,text,stamp=100,source='one',actor='liu',role='assistant'):
        return {'id':source,'content':text,'speaker':events.actors.ROLES.get(actor,''),
                'created_at':stamp,'role':role}

    def rows(self):
        with Session(self.store.engine) as db:
            return list(db.scalars(select(events.Event).order_by(events.Event.occurred_at)))

    def test_move_updates_state_position_and_history_from_same_source(self):
        record=self.record('小柳：（走进厨房）')
        events.capture('owner','world','liu',record)
        data=self.store.read('owner','world')
        self.assertEqual(data['role_positions']['liu']['place'],'厨房')
        self.assertTrue(data['role_states']['liu']['status'].startswith('厨房·'))
        self.assertEqual(data['role_states']['liu']['source_id'],'one')
        self.assertEqual(events.history('owner','world','liu','厨房')[0]['source_id'],'one')
        self.assertEqual(events.history('owner','world','wan','厨房'),[])
        events.capture('owner','world','liu',record)
        self.assertEqual(len(self.rows()),1)

    def test_old_evidence_is_retained_but_cannot_move_current_position_back(self):
        events.capture('owner','world','liu',self.record('小柳：（走进厨房）',200,'new'))
        events.capture('owner','world','liu',self.record('小柳：（走进卧室）',100,'old'))
        data=self.store.read('owner','world')
        self.assertEqual(data['role_positions']['liu']['place'],'厨房')
        self.assertTrue(data['role_states']['liu']['status'].startswith('厨房·'))
        self.assertEqual(len(self.rows()),2)

    def test_multiple_moves_follow_source_order_even_if_model_reverses_output(self):
        record=self.record('小柳：（走进厨房，随后回到卧室）')
        events.capture('owner','world','liu',record)
        self.assertEqual(self.store.read('owner','world')['role_positions']['liu']['place'],'卧室')
        events.apply_model('owner','world','liu',[record],{'events':[
            {'source_id':'one','actor':'liu','evidence':'回到卧室','status':'卧室·休息','position':{'place':'卧室'}},
            {'source_id':'one','actor':'liu','evidence':'走进厨房','status':'厨房·活动','position':{'place':'厨房'}}]})
        data=self.store.read('owner','world')
        self.assertEqual(data['role_states']['liu']['status'],'卧室·休息')
        self.assertEqual(data['role_positions']['liu']['place'],'卧室')
        events.apply_model('owner','world','liu',[record],{'events':[{
            'source_id':'one','actor':'liu','evidence':'走进厨房，随后回到卧室',
            'status':'厨房·活动','position':{'place':'卧室'}}]})
        self.assertEqual(self.store.read('owner','world')['role_states']['liu']['status'],'卧室·休息')

    def test_new_activity_does_not_erase_position_and_user_you_refers_to_private_actor(self):
        events.capture('owner','world','liu',self.record('小柳：（走进厨房）'))
        record=self.record('小柳：（洗杯子）',200,'wash')
        events.apply_model('owner','world','liu',[record],{'events':[{
            'source_id':'wash','actor':'liu','evidence':'洗杯子','status':'厨房·洗杯子'}]})
        self.assertEqual(continuity.context('owner','world','liu')['own_position']['place'],'厨房')
        events.capture('owner','world','liu',self.record('（走到你旁边）',300,'user-move',role='user'))
        self.assertEqual(self.store.read('owner','world')['role_positions']['user']['relative_to'],'liu')

    def test_plans_and_negated_actions_do_not_become_positions(self):
        for text in ('小柳：（准备走进厨房）','小柳：（没有走进厨房）','小柳：（没走进厨房）',
                     '小柳：（想走进厨房）','小柳：我已经在厨房。'):
            events.capture('owner','world','liu',self.record(text))
        self.assertNotIn('role_positions',self.store.read('owner','world'))

    def test_model_understands_new_wording_without_fixed_action_keyword(self):
        from backend.app.world import scene_updates
        from backend.app.runtime import role_actions
        record=self.record('小柳：（踱入露台，倚着栏杆）')
        answer={'events':[{'source_id':'one','actor':'liu','evidence':'踱入露台，倚着栏杆',
            'status':'露台·倚栏','position':{'place':'露台'}}]}
        fake=MagicMock();fake.__enter__.return_value=fake
        fake.chat.completions.create.return_value=S(choices=[S(message=S(content=json.dumps(answer)))])
        with role_actions.window('owner','world','liu'),patch('openai.OpenAI',return_value=fake),patch('backend.app.infrastructure.model_usage.config',return_value={'api_key':'fake','model':'fake'}):
            result=scene_updates.update_scene([record],cfg_override={'api_key':'fake','model':'fake'})
        fake.chat.completions.create.assert_called_once()
        self.assertEqual(result['小柳'],'露台·倚栏')
        self.assertEqual(continuity.context('owner','world','liu')['own_position']['place'],'露台')

    def test_model_cannot_invent_source_location_actor_or_delivery(self):
        record=self.record('小柳：（倚着栏杆）')
        bad=[{'source_id':'missing','actor':'liu','evidence':'倚着栏杆','status':'露台·休息'},
             {'source_id':'one','actor':'wan','evidence':'倚着栏杆','status':'露台·休息'},
             {'source_id':'one','actor':'liu','evidence':'倚着栏杆','position':{'place':'厨房'}},
             {'source_id':'one','actor':'liu','evidence':'倚着栏杆','status':'厨房·洗碗'}]
        events.apply_model('owner','world','liu',[record],{'events':bad})
        self.assertEqual(self.rows(),[])
        self.assertNotIn('role_states',self.store.read('owner','world'))

    def test_model_cannot_turn_part_of_a_future_plan_into_completed_action(self):
        record=self.record('小柳明天走进厨房',role='user')
        events.apply_model('owner','world','liu',[record],{'events':[{
            'source_id':'one','actor':'liu','evidence':'走进厨房',
            'status':'厨房·活动','position':{'place':'厨房'}}]})
        self.assertEqual(self.rows(),[])

    def test_failed_projection_rolls_back_event_and_state_together(self):
        with patch.object(events,'_project',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                events.capture('owner','world','liu',self.record('小柳：（走进厨房）'))
        self.assertEqual(self.rows(),[])
        self.assertNotIn('role_positions',self.store.read('owner','world'))

    def test_life_fact_and_physical_state_share_the_same_source_history(self):
        from backend.app.world import world_events
        world_events.capture('owner','world','liu',self.record('小柳：（走进厨房，开火煮面）',100,'cooking'))
        data=self.store.read('owner','world')
        self.assertEqual(data['household_meal']['state'],'cooking')
        self.assertEqual(data['role_positions']['liu']['place'],'厨房')
        world_events.capture('owner','world','liu',self.record('小柳：（关火，盛好面）面好了。',200,'ready'))
        self.assertEqual(self.store.read('owner','world')['household_meal']['state'],'ready')
        self.assertEqual(len([r for r in self.rows() if r.kind=='physical_fact']),2)
        self.assertEqual({r.source_id for r in self.rows()},{'cooking','ready'})

    def test_semantic_retry_is_idempotent_and_does_not_erase_current_state(self):
        record=self.record('小柳：（走进厨房，洗杯子）')
        answer={'events':[{'source_id':'one','actor':'liu','evidence':'走进厨房，洗杯子',
            'status':'厨房·洗杯子','position':{'place':'厨房'}}]}
        events.apply_model('owner','world','liu',[record],answer)
        events.apply_model('owner','world','liu',[record],answer)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.store.read('owner','world')['role_states']['liu']['validity'],'confirmed')

    def test_timeout_preserves_last_observation_and_records_uncertainty(self):
        now=time.time()
        events.capture('owner','world','liu',self.record('小柳：（走进厨房）',now,'move'))
        first=realtime_world.sync('owner','world')
        last=first['role_states']['liu']['last_confirmed_status']
        events.sync_time('owner','world',now+3*3600)
        facts,_,_=actor_context.build('owner','world','liu','你现在在哪')
        self.assertEqual(facts['self']['validity'],'stale')
        self.assertEqual(facts['self']['last_confirmed_status'],last)
        self.assertEqual(len([r for r in self.rows() if r.kind=='freshness']),1)
        self.assertNotIn('已完成',facts['self']['last_confirmed_status'])

    def test_history_outlives_bounded_cache_and_is_scoped_by_user_and_world(self):
        items=[]
        for i in range(220):
            record=self.record('小柳：（拿起旧礼物）' if i==0 else '小柳：（整理杂物）',i+1,str(i))
            items.append(events.make('owner','world',record,'liu','liu','action',{
                'text':record['content'],'known_to':['liu'],'witnesses':['liu']}))
        events.commit('owner','world',items)
        self.assertEqual(len(self.store.read('owner','world')['witnessed_events']),200)
        self.assertTrue(any('旧礼物' in e['text'] for e in events.history('owner','world','liu','旧礼物')))
        self.store.ensure('other','other-world',{})
        self.assertEqual(events.history('other','other-world','liu','旧礼物'),[])
        with self.assertRaises(PermissionError):events.history('other','world','liu','旧礼物')

    def test_concurrent_retry_keeps_one_event_and_one_projection(self):
        record=self.record('小柳：（走进厨房）')
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _:events.capture('owner','world','liu',record),range(6)))
        self.assertEqual(len(self.rows()),1)

    def test_peer_delivery_and_receipt_are_atomic_and_private(self):
        peer_chat.send('owner','world','yan','liu','你好','hello',schedule=False)
        facts,hist,receipts=actor_context.build('owner','world','liu','她发消息了吗')
        self.assertEqual(receipts[0]['status'],'sent')
        self.assertEqual(receipts[0]['sender'],'yan')
        self.assertEqual(actor_context.build('owner','world','wan','她发消息了吗')[2],[])
        def fail(*args):raise RuntimeError('rollback')
        with self.assertRaises(RuntimeError):
            peer_chat.send('owner','world','yan','liu','不能发送','failed',schedule=False,commit_hook=fail)
        self.assertFalse(any(r.source_id=='failed' for r in self.rows()))

    def test_explicit_reset_clears_event_ledger_and_prevents_old_replay(self):
        events.capture('owner','world','liu',self.record('小柳：（走进厨房）'))
        story_reset.reset_story('owner','world',backup=False)
        self.assertEqual(self.rows(),[])
        events.capture('owner','world','liu',self.record('小柳：（走进厨房）'))
        self.assertEqual(self.rows(),[])

    def test_migration_reopen_preserves_event_history(self):
        events.capture('owner','world','liu',self.record('小柳：（走进厨房）'))
        reopened=storage.Store(str(self.store.engine.url))
        try:
            with Session(reopened.engine) as db:self.assertEqual(db.scalar(select(func.count()).select_from(events.Event)),1)
        finally:reopened.engine.dispose()

    def test_legacy_history_is_adopted_once_without_overwriting_current_state(self):
        self.store.mutate('owner','world',lambda d:d.update(witnessed_events=[{
            'id':'legacy','source_id':'old-message','actor':'yan','text':'拿起礼物','created_at':1,
            'witnesses':['yan'],'heard_by':['liu'],'channel':'group'}]))
        events.adopt_legacy('owner','world');events.adopt_legacy('owner','world')
        self.assertEqual(len(self.rows()),1)
        self.assertIn('不能当作本人目击',events.history('owner','world','liu','礼物')[0]['basis'])
        self.assertNotIn('role_states',self.store.read('owner','world'))
