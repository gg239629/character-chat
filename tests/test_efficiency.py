import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import patch,AsyncMock,MagicMock
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.runtime import group_coordinator
from backend.app.memory import long_memory
from backend.app.infrastructure import model_usage
from backend.app.runtime import role_actions
from backend.app.memory import role_memory
import server
from backend.app.infrastructure import storage


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'test.db').as_posix())
        self.store.ensure('owner','default',{'recent_log':[],'scene_state':{'小婉':'卧室·看书'}})
        self.patch=patch.object(storage,'_STORE',self.store);self.patch.start()
        for role in actors.ROLES:
            with actors.role_scope('owner','default',role):chat_history.bootstrap()

    def tearDown(self):
        self.patch.stop();self.store.engine.dispose();self.tmp.cleanup()

    def test_memory_excludes_current_ids_compacts_metadata_and_preserves_other_channel(self):
        chat_history.bootstrap();chat_history.append('public',0,'assistant','小婉：我们周末去植物园吧。','小婉')
        original=chat_history.messages()[-1]
        with actors.role_scope('owner','default','wan'):chat_history.append('private',0,'user','只告诉小婉的植物园秘密')
        memory=role_memory.remember('owner','default','wan','植物园',True,exclude_ids=[original['id']],exclude_texts=[original['content']])
        rendered=role_memory.render(memory)
        self.assertNotIn(original['content'],rendered);self.assertIn('只告诉小婉',rendered)
        for key in ('conversation_id','visibility','audience','actor_id','source_ids'):self.assertNotIn('"'+key+'"',rendered)
        raw=json.dumps(memory['events'],ensure_ascii=False)
        self.assertLess(len(rendered.split('\n')[-1]),len(raw))

    def test_peer_question_changes_retrieval_and_budget_is_adaptive(self):
        messages=[{'role':'user','content':'周末去哪里？'},{'role':'assistant','content':'小研：小婉，你上次说的兰花展具体几点？'}]
        query=role_memory.query_for(messages)
        self.assertIn('兰花展',query);self.assertIn('周末去哪里',query)
        self.assertGreater(role_memory.adaptive_budget(query),role_memory.adaptive_budget('早上好'))

    def test_ordinary_chat_uses_lexical_recall_and_marks_private_evidence(self):
        from backend.app.memory import retrieval
        with actors.role_scope('owner', 'default', 'wan'):
            storage.write_document('important_facts', [{
                'id': 'own', 'text': '植物园周年展览', 'key': '植物园展览',
            }])
        with patch.object(retrieval, 'rank', side_effect=AssertionError('unexpected semantic recall')):
            memory = role_memory.remember('owner', 'default', 'wan', '植物园展览', True, semantic=False)
        text = role_memory.render(memory, group=True)
        self.assertIn('植物园周年展览', text)
        self.assertIn('未经授权不得在群聊公开', text)

    def test_correction_can_match_the_original_topic_by_fact_key(self):
        storage.write_document('important_facts', [
            {'id': 'original', 'key': '植物园周末计划', 'text': '周六出发', 'status': 'superseded'},
            {'id': 'updated', 'key': '植物园周末计划', 'text': '已经取消', 'status': 'cancelled'},
        ])
        memory = role_memory.remember('owner', 'default', 'wan', '植物园周末计划', True,
                                      recall_budget=1200, semantic=False)
        self.assertEqual([event['id'] for event in memory['recall']], ['updated'])

    def test_prompt_history_retrieves_old_current_channel_evidence_without_replaying_recent_records(self):
        chat_history.append('old-booking', 0, 'user', '植物园预约编号 Q7M9')
        chat_history.append('recent-note', 0, 'user', '后勤事项已经更新')
        memory = role_memory.remember('owner', 'default', 'wan', '植物园预约编号', True,
                                      recall_budget=1200, semantic=False, include_recent_current=False)
        self.assertTrue(any('Q7M9' in event['text'] for event in memory['recall']))
        self.assertFalse(any('后勤事项' in event['text'] for event in memory['events']))

    def test_prompt_compaction_persists_once_and_keeps_canonical_messages(self):
        from backend.app.memory import prompt_history
        with role_actions.window('owner', 'default', 'wan') as channel:
            for index in range(4):
                chat_history.append(str(index), 0, 'user', f'{index}: ' + '记录' * 175)
                chat_history.append(str(index), 1, 'assistant', '已记录', '小婉')
            current = [{'role': 'user', 'content': '好'}]
            system = [{'role': 'system', 'content': '固定人设'}]
            with (
                patch.dict('os.environ', {'CHAT_CONTEXT_INPUT_TOKENS': '3200'}),
                patch.object(prompt_history, 'counter', return_value=len),
                patch.object(prompt_history, '_summary', return_value='已确认的历史事实') as summary,
            ):
                first = prompt_history.prepare('owner', 'default', 'wan', False, current, system, {})
                second = prompt_history.prepare('owner', 'default', 'wan', False, current, system, {})
            state = self.store.read('owner', channel)['prompt_history']['wan']
            self.assertEqual(state['summaries'], ['已确认的历史事实'])
            self.assertEqual(first[:3], second[:3])
            self.assertEqual(len(chat_history.messages()), 8)
            summary.assert_called_once()

    def test_oversized_active_turn_does_not_start_compaction(self):
        from backend.app.memory import prompt_history
        with (
            role_actions.window('owner', 'default', 'wan'),
            patch.dict('os.environ', {'CHAT_CONTEXT_INPUT_TOKENS': '3200'}),
            patch.object(prompt_history, 'counter', return_value=len),
            patch.object(prompt_history, '_summary', side_effect=AssertionError('unexpected summary call')),
        ):
            for index in range(4):
                chat_history.append(str(index), 0, 'user', f'{index}: ' + '记录' * 175)
                chat_history.append(str(index), 1, 'assistant', '已记录', '小婉')
            with self.assertRaises(ValueError):
                prompt_history.prepare('owner', 'default', 'wan', False,
                                       [{'role': 'user', 'content': '字' * 3000}],
                                       [{'role': 'system', 'content': '固定人设'}], {})

    def test_actor_prompt_deduplicates_current_transcript_and_strips_internal_ids(self):
        chat_history.bootstrap();chat_history.append('original',0,'user','非常独特的当前窗口消息')
        message=chat_history.messages()[-1]
        with patch.object(server,'load_config',return_value={'api_key':'fake','model':'test'}):
            _,payload=server.actor_payload('wan',[message],True)
        text=json.dumps(payload,ensure_ascii=False)
        self.assertEqual(text.count('非常独特的当前窗口消息'),1)
        self.assertTrue(all(set(item)=={'role','content'} for item in payload))

    def test_actor_prefix_stays_fixed_across_topics_and_greetings_skip_extra_recall(self):
        root = Path(self.tmp.name)
        profile = root / 'wan.md'
        profile.write_text('身份：测试角色。\n\n性格：开朗。\n\n兰花在植物园。\n\n周末喜欢弹钢琴。', encoding='utf-8')
        with (
            patch.object(server, 'MEMORY_MD', root / 'shared.md'),
            patch.object(role_memory, 'remember', wraps=role_memory.remember) as remember,
        ):
            _, greeting = server.actor_payload('wan', [{'role': 'user', 'content': '早上好！'}], True)
            remember.assert_not_called()
            _, plants = server.actor_payload('wan', [{'role': 'user', 'content': '植物园的兰花'}], True)
            _, music = server.actor_payload('wan', [{'role': 'user', 'content': '喜欢弹钢琴吗'}], True)
        self.assertEqual(greeting[:2], plants[:2])
        self.assertEqual(plants[:2], music[:2])
        self.assertIn('身份：测试角色', plants[0]['content'])
        self.assertNotIn('植物园', plants[0]['content'])
        self.assertTrue(any('兰花在植物园' in m['content'] for m in plants[2:]))
        self.assertTrue(any('周末喜欢弹钢琴' in m['content'] for m in music[2:]))

    def test_actor_recall_survives_long_chat_and_respects_corrections_and_permissions(self):
        self.store.mutate('owner', 'default', lambda data: data.update(memory_policy=2, important_facts=[
            {'id': 'old', 'key': '植物园约定', 'text': '植物园约定周六出发', 'status': 'superseded', 'created_at': 1},
            {'id': 'new', 'key': '植物园约定', 'text': '取消植物园约定', 'status': 'cancelled',
             'evidence': '植物园约定取消，周末不去了。', 'created_at': 2, 'source_end': '2026-11-01T09:30:00+08:00'},
        ]))
        with actors.role_scope('owner', 'default', 'yan'):
            storage.write_document('important_facts', [
                {'id': 'private', 'text': '植物园约定密语紫色手套', 'status': 'active'},
            ])
        with role_actions.window('owner', 'default', 'group'):
            for index in range(50):
                chat_history.append(f'turn-{index}', 0, 'user', f'闲聊第{index}轮 ' + '日常' * 210)
                chat_history.append(f'turn-{index}', 1, 'assistant', '好的' * 210, '小婉')
            _, payload = server.actor_payload('wan', [
                {'role': 'user', 'content': '上次植物园约定后来怎么处理的？'},
            ], True)
            _, generic_recall = server.actor_payload('wan', [
                {'role': 'user', 'content': '上次答应我什么？'},
            ], True)
        evidence = next(m['content'] for m in payload if m['content'].startswith('本轮关键记忆：'))
        self.assertIn('cancelled', evidence)
        self.assertIn('植物园约定取消，周末不去了。', evidence)
        self.assertIn('2026-11-01T09:30:00+08:00', evidence)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('周六出发', text)
        self.assertNotIn('紫色手套', text)
        from backend.app.memory import prompt_history
        count = prompt_history.counter()
        self.assertLessEqual(sum(4 + count(m['content']) for m in payload), 24576)
        self.assertTrue(any('植物园约定取消，周末不去了。' in m['content'] for m in generic_recall))

    def test_direct_question_routes_without_coordinator_model_and_actor_still_decides(self):
        token=group_coordinator.discussion_state.set({'last_speaker':'wan','messages_this_exchange':1,'declined_current_context':[]})
        try:
            with patch.object(role_actions,'ask_json',new=AsyncMock()) as ask:
                result=asyncio.run(group_coordinator.plan([{'role':'assistant','content':'小婉：小研，你觉得几点合适？'}],{}))
                self.assertEqual(result,['yan']);ask.assert_not_awaited()
        finally:group_coordinator.discussion_state.reset(token)

    def test_compact_coordinator_is_bounded_and_excludes_internal_message_ids(self):
        histories=[{'id':'internal-'+str(i),'role':'user' if i%2==0 else 'assistant','content':'普通公开话题。'*200} for i in range(80)]
        captured=[]
        async def decide(cfg,system,context):captured.append(context);return {'order':[]}
        with role_actions.window('owner','default','group'),patch.object(server,'load_config',return_value={'api_key':'fake','model':'test'}),patch.object(role_actions,'ask_json',new=decide):
            asyncio.run(group_coordinator.plan(histories,{}))
        context=captured[0]
        self.assertLessEqual(sum(len(m['content']) for m in context['group_messages']),4200)
        self.assertNotIn('internal-',json.dumps(context))

    def test_scene_without_action_does_not_call_provider_but_action_still_does(self):
        with role_actions.window('owner','default','group'),patch('openai.OpenAI') as provider:
            state=server.update_scene_state([{'role':'user','content':'早上好'},{'role':'assistant','content':'小婉：我在外面跑步。'}])
            self.assertEqual(state['小婉'],'卧室·看书');provider.assert_not_called()
        fake=MagicMock();fake.__enter__.return_value=fake
        def extract(**kwargs):
            source=json.loads(kwargs['messages'][1]['content'])['messages'][0]['source_id']
            return S(choices=[S(message=S(content=json.dumps({'events':[{
                'source_id':source,'actor':'wan','evidence':'走进厨房煮粥',
                'status':'厨房·煮粥','position':{'place':'厨房'}}]})))])
        fake.chat.completions.create.side_effect=extract
        with role_actions.window('owner','default','group'),patch('openai.OpenAI',return_value=fake):
            scene=server.update_scene_state([{'role':'assistant','content':'小婉：（走进厨房煮粥）'}],cfg_override={'api_key':'fake','model':'test','base_url':''})
            self.assertEqual(scene['小婉'],'厨房·煮粥');fake.chat.completions.create.assert_called_once()

    def test_usage_never_persists_content_is_batched_isolated_and_cache_is_counted(self):
        with model_usage.Measure('character',{'model':'test'},[{'role':'user','content':'must-not-store-secret'}]) as meter:
            meter.observe(S(usage=S(prompt_tokens=100,completion_tokens=20,prompt_tokens_details=S(cached_tokens=70))))
            meter.output('also-not-store')
        with Session(self.store.engine) as db:self.assertEqual(list(db.scalars(select(model_usage.Usage))),[])
        model_usage.flush(force=True)
        result=model_usage.report('owner')['categories']['character']
        self.assertEqual((result['calls'],result['input_tokens'],result['cached_tokens']),(1,100,70))
        self.assertEqual(model_usage.report('another-user')['categories'],{})
        self.assertNotIn('must-not-store-secret',str(result))
        self.assertNotIn('content',model_usage.Usage.__table__.columns)

    def test_unknown_usage_is_not_fabricated_and_failed_calls_are_counted(self):
        with self.assertRaises(ValueError):
            with model_usage.Measure('action',{'model':'test'},[]) as meter:raise ValueError('private-error')
        result=model_usage.report('owner')['categories']['action']
        self.assertEqual(result['failed'],1);self.assertEqual(result['calls_with_token_usage'],0)

    def test_model_cache_rate_counts_only_calls_reporting_cache_usage(self):
        for model, input_tokens, cached_tokens in [
            ('a', 100, 70), ('a', 200, 0), ('a', 500, None), ('b', 50, None),
        ]:
            with model_usage.Measure('character', {'model': model}, []) as meter:
                meter.observe(S(usage=S(prompt_tokens=input_tokens, prompt_cache_hit_tokens=cached_tokens)))
        report = model_usage.report('owner')['character_cache_by_model']
        self.assertEqual(report['a']['calls'], 3)
        self.assertEqual(report['a']['reported_calls'], 2)
        self.assertEqual(report['a']['input_tokens'], 300)
        self.assertAlmostEqual(report['a']['hit_rate'], 70 / 300)
        self.assertIsNone(report['b']['hit_rate'])

    def test_stream_usage_compatibility_fallback_keeps_reply_and_real_usage(self):
        class Unsupported(Exception):status_code=400
        seen=[]
        async def create(**kwargs):
            seen.append(kwargs)
            if 'stream_options' in kwargs:raise Unsupported('stream_options is not supported')
            async def chunks():
                yield S(choices=[S(delta=S(content='小研：早上好。'))])
                yield S(choices=[],usage=S(prompt_tokens=99,completion_tokens=8,prompt_tokens_details=S(cached_tokens=30)))
            return chunks()
        async def close():pass
        model=S(chat=S(completions=S(create=create)),close=close)
        with patch.object(server,'load_config',return_value={'api_key':'fake','model':'test','base_url':'','temperature':.5,'max_tokens':100}),patch.object(server,'make_client',return_value=model),patch.dict('os.environ',{'MODEL_STREAM_USAGE':'true','RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()}),TestClient(server.app) as client:
            response=client.post('/api/chat',headers={'X-Chat-Channel':'yan'},json={'request_id':'usage-fallback','messages':[{'role':'user','content':'早上好'}]})
            self.assertEqual(response.status_code,200);self.assertIn('早上好',response.json()['reply'])
        self.assertEqual(len(seen),2);self.assertNotIn('stream_options',seen[1])
        result=model_usage.report('owner')['categories']['character']
        self.assertEqual((result['input_tokens'],result['output_tokens'],result['cached_tokens']),(99,8,30))

    def test_auxiliary_override_does_not_change_character_configuration(self):
        cfg={'model':'original','api_key':'key','base_url':'https://original.invalid'}
        with patch.dict('os.environ',{'AUX_MODEL':'fast','AUX_API_KEY':'aux-key','AUX_BASE_URL':'https://aux.invalid'}),patch.object(model_usage,'group_config',return_value={'model':'group'}):
            self.assertEqual(model_usage.config('coordinator',cfg)['model'],'group')
            self.assertEqual(model_usage.config('character',cfg),cfg)
        self.assertEqual(cfg['model'],'original')

    def test_fact_replacement_requires_explicit_original_evidence_and_keeps_history(self):
        records=[{'id':str(i),'who':'他' if i%2==0 else '小婉','text':'植物园改到周日去。' if i==0 else '普通日常聊天','saved_at':'2026-11-01T08:00:00+08:00'} for i in range(50)]
        storage.write_document('pending_archive',records)
        storage.write_document('important_facts',[{'id':'old','key':'植物园:日期','text':'植物园周六去','status':'active'}])
        response={'summary':'原本植物园周六出行，现明确改到周日，尚未出发。','facts':[{'text':'植物园改到周日去','key':'植物园:日期','status':'active','evidence':'植物园改到周日去。'}]}
        fake=MagicMock();fake.__enter__.return_value=fake;fake.chat.completions.create.return_value=S(choices=[S(message=S(content=json.dumps(response,ensure_ascii=False)))])
        with patch('openai.OpenAI',return_value=fake):self.assertTrue(long_memory.refresh_summary({'api_key':'fake','model':'test'}))
        facts=storage.read_document('important_facts',[])
        self.assertEqual(facts[0]['status'],'superseded');self.assertEqual(facts[1]['status'],'active')
        self.assertEqual(len(storage.read_document('original_backups',[])),50)
        memory=role_memory.remember('owner','default','wan','植物园日期',True)
        self.assertNotIn('植物园周六去',role_memory.render(memory))
        self.assertIn('植物园改到周日去',role_memory.render(memory))
