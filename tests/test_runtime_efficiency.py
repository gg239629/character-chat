"""Neutral regressions for notification isolation, task coalescing and metering."""
import asyncio
import time
import unittest
from unittest.mock import patch
from sqlalchemy import select,update
from sqlalchemy.orm import Session
import test_home_settings as fixtures
import server
from backend.app.infrastructure import storage,events,maintenance,model_usage
from backend.app.runtime import graph,role_actions
from backend.app.core import services
from backend.app.mcp import access,server as connector
from backend.app.memory import retrieval
from backend.app.conversations import chat_history

class RuntimeEfficiencyTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown

    def test_events_are_account_scoped_coalesced_and_commit_only(self):
        async def check():
            async with events.subscribe('owner') as owner,events.subscribe('other') as other:
                with Session(self.store.engine) as db:
                    row=db.get(storage.Conversation,'default');row.data={**row.data,'marker':'rolled back'}
                    db.flush();db.rollback()
                await asyncio.sleep(.02)
                self.assertTrue(owner.empty())
                self.store.mutate('owner','default',lambda data:data.update(marker='committed'))
                events.publish('owner');events.publish('owner')
                await asyncio.sleep(.02)
                self.assertEqual(owner.qsize(),1);self.assertTrue(other.empty())
        asyncio.run(check())

    def test_graph_resources_and_compiled_graph_are_reused(self):
        a=graph._resource(self.store.engine);b=graph._resource(self.store.engine)
        self.assertIs(a,b);self.assertIs(graph.compiled(a),graph.compiled(b))
        self.store.engine.dispose()
        self.assertNotIn(self.store.engine,graph._resources)

    def test_scene_batches_keep_every_new_action_and_private_channels_separate(self):
        with role_actions.window('owner','default','wan'):
            chat_history.append('first',0,'user','小婉回到客厅了。')
            first=chat_history.messages(1)[-1]['created_at']
            maintenance.enqueue('owner','default','wan','first',first)
            chat_history.append('second',0,'user','小婉坐下看书。')
            second=chat_history.messages(1)[-1]['created_at']
            maintenance.enqueue('owner','default','wan','second',second)
        maintenance.enqueue('owner','default','yan','other',second+.001)
        with Session(self.store.engine) as db:
            rows=list(db.scalars(select(maintenance.Job).where(maintenance.Job.kind=='scene')))
        self.assertEqual(sum(r.status=='pending' for r in rows),2)
        newest=next(r for r in rows if r.channel=='wan' and r.status=='pending')
        with patch.object(server,'update_scene_state',return_value={}) as update:
            maintenance.process(newest.id)
            contents=[m['content'] for m in update.call_args.args[0]]
            self.assertEqual(contents,['小婉回到客厅了。','小婉坐下看书。'])

    def test_mcp_token_budget_reserves_settles_and_blocks_before_model_call(self):
        key=access.create('owner','budget',['chat','read','wan'],daily_tokens=500)
        actor=access.identify(key['token']);connector.ensure_world(actor)
        with role_actions.window('owner',actor.world,'wan'):
            hold=access.begin_model([{'role':'user','content':'hello'}],100)
            self.assertGreater(access.list_keys('owner')[0]['reserved_tokens'],0)
            access.end_model(hold,80,20);access.end_model(hold,80,20)
            self.assertEqual(access.list_keys('owner')[0]['used_tokens'],100)
            with self.assertRaises(access.BudgetExceeded):access.begin_model([{'role':'user','content':'x'*600}],100)
        access.revoke('owner',key['id'])
        with role_actions.window('owner',actor.world,'wan'):
            with self.assertRaises(access.BudgetExceeded):access.begin_model([],1)

    def test_expired_token_holds_are_conservatively_accounted(self):
        key=access.create('owner','budget',['chat','wan'],daily_tokens=10000)
        actor=access.identify(key['token']);connector.ensure_world(actor)
        with role_actions.window('owner',actor.world,'wan'):
            previous=access.begin_model([],100)
            with Session(self.store.engine) as db:
                db.execute(update(access.TokenHold).values(expires_at=0));db.commit()
            next_hold=access.begin_model([],100)
            access.end_model(next_hold,1,1)
            self.assertGreater(access.list_keys('owner')[0]['used_tokens'],100)
            self.assertEqual(access.list_keys('owner')[0]['estimated_calls'],1)
            access.end_model(previous,1,1)
            self.assertEqual(access.list_keys('owner')[0]['reserved_tokens'],0)

    def test_index_background_warmup_does_not_block_and_reuses_ready_corpus(self):
        import threading
        gate=threading.Event();started=threading.Event();fake=object()
        records=[{'text':'公开的植物园约定'}]
        cfg={'api_key':'fake','embedding_model':'embed','base_url':''}
        def build(*args):started.set();gate.wait(5);return fake
        with patch.dict('os.environ',{'RUN_MAINTENANCE_WORKER':'true'}),patch.object(retrieval,'build_index',side_effect=build) as builder:
            try:
                self.assertEqual(retrieval.hybrid_rank('目的地',records,[],cfg),[])
                self.assertTrue(started.wait(2))
                self.assertEqual(retrieval.hybrid_rank('目的地',records,[],cfg),[])
                self.assertEqual(builder.call_count,1)
            finally:gate.set();retrieval.wait_pending();retrieval.clear_indexes('owner')

    def test_domain_services_do_not_expose_http_app_or_other_capabilities(self):
        with self.assertRaises(AttributeError):getattr(services.models(),'app')
        with self.assertRaises(AttributeError):getattr(services.world(),'make_client')
        with patch.object(server,'load_config',return_value={'model':'patched'}):
            self.assertEqual(services.models().load_config()['model'],'patched')

    def test_daily_dialogue_regressions_preserve_new_facts_and_reject_planned_moves(self):
        from backend.app.world import scene_labels
        from backend.app.characters import reply_quality
        cases=[
            ('小研：（准备回到家里）',None),
            ('小研：（假装到家了）',None),
            ('小研：（进了家门）','家里·刚进门'),
        ]
        for text,expected in cases:
            with self.subTest(text=text):
                self.assertEqual(scene_labels.physical_transition([{'role':'assistant','content':text}],'小研',True),expected)
        self.assertTrue(reply_quality.redundant('小研：还没吃上呢。',['小研：还没吃上呢。']))
        self.assertFalse(reply_quality.redundant('小研：蛋改成4个。',['小研：蛋改成3个。']))
        self.assertIsNone(scene_labels.physical_transition([{'role':'assistant','content':'小婉：（进了家门）'}],'小研',True))
