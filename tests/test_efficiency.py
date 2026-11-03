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

    def test_actor_prompt_deduplicates_current_transcript_and_strips_internal_ids(self):
        chat_history.bootstrap();chat_history.append('original',0,'user','非常独特的当前窗口消息')
        message=chat_history.messages()[-1]
        with patch.object(server,'load_config',return_value={'api_key':'fake','model':'test'}):
            _,payload=server.actor_payload('wan',[message],True)
        text=json.dumps(payload,ensure_ascii=False)
        self.assertEqual(text.count('非常独特的当前窗口消息'),1)
        self.assertTrue(all(set(item)=={'role','content'} for item in payload))

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
