import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from backend.app.characters import actors
from backend.app.conversations import chat_history
from backend.app.infrastructure import secrets_store
import server
from backend.app.infrastructure import storage


class ActorTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import AsyncMock
        from backend.app.characters import actors
        self.coordinator_patch=patch.object(server.group_coordinator,'plan',new=AsyncMock(side_effect=lambda messages,configs:[role for role in actors.candidates(messages) if not any(m.get('role')=='assistant' and m.get('content','').startswith(actors.ROLES[role]+'：') for m in server.recent_rounds(messages,1))]))
        self.coordinator_patch.start()
        self.addCleanup(self.coordinator_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store('sqlite:///' + (Path(self.tmp.name)/'chat.db').as_posix())
        self.store.ensure('owner','default',{'scene_state':{'在场人物':'小婉、小研、小柳'},'recent_log':[]})
        self.patches = [patch.object(storage,'_STORE',self.store),
            patch.dict('os.environ',{'RUN_MAINTENANCE_WORKER':'false','APP_ENCRYPTION_KEY':Fernet.generate_key().decode()})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.store.engine.dispose()
        self.tmp.cleanup()

    def test_schedule_respects_target_and_presence(self):
        self.assertEqual(actors.choose([{'role':'user','content':'小研你呢'}],{}),['yan'])
        self.assertEqual(actors.choose([{'role':'user','content':'你们各自说说'}],{'在场人物':'小婉、小柳'}),['wan','liu'])
        self.assertEqual(actors.normalize_reply('小研：自己的话\n小柳：不该说的话','小研'),'小研：自己的话')

    def test_encrypted_keys_and_role_config(self):
        with TestClient(server.app) as client:
            client.post('/api/settings',headers={'X-Chat-Channel':'yan'},json={'model':'yan-model','api_key':'private-key-test','persona':'喜欢植物','relationship':'一起种过兰花'})
            identity=actors.role_id('default','yan')
            saved=self.store.read('owner',identity)['settings']['api_key']
            self.assertTrue(saved.startswith(secrets_store.PREFIX))
            self.assertNotIn('private-key-test',saved)
            cfg,payload=self._payload('yan',[{'role':'user','content':'你好'}])
            self.assertEqual(cfg['model'],'yan-model')
            self.assertEqual(cfg['api_key'],'private-key-test')
            self.assertIn('喜欢植物',json.dumps(payload,ensure_ascii=False))
            self.assertIn('一起种过兰花',json.dumps(payload,ensure_ascii=False))
            self.assertNotIn('private-key-test',client.get('/api/settings',headers={'X-Chat-Channel':'yan'}).text)

    def _payload(self, role, history):
        token=server.chat_world.set({'id':'default','channel':'group','data':self.store.read('owner','default')})
        try:
            return server.actor_payload(role,history,True)
        finally:
            server.chat_world.reset(token)

    def test_each_actor_only_receives_their_private_memory(self):
        for role in actors.ROLES:
            with actors.role_scope('owner','default',role):
                storage.append_log([{'who':'他','text':'private-secret-'+role}])
        _,payload=self._payload('yan',[{'role':'user','content':'大家好'}])
        text=json.dumps(payload,ensure_ascii=False)
        self.assertIn('private-secret-yan',text)
        self.assertNotIn('private-secret-wan',text)
        self.assertNotIn('private-secret-liu',text)

    def test_sequential_models_autosave_and_idempotency(self):
        calls=[]
        def model(cfg):
            async def create(**kwargs):
                calls.append((cfg['model'],kwargs['messages']))
                name=next(name for name in actors.ROLES.values() if '你只扮演'+name in kwargs['messages'][-1]['content'])
                async def chunks():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=name+'：'+{'小婉':'周六一起看电影。','小研':'我想去书店选一本书。','小柳':'我更想去植物园看花。'}[name]))])
                return chunks()
            async def close():
                pass
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
        with TestClient(server.app) as client:
            for role in actors.ROLES:
                client.post('/api/settings',headers={'X-Chat-Channel':role},json={'model':'model-'+role,'api_key':'fake'})
            body={'request_id':'test-turn','messages':[{'role':'user','content':'你们周六想去哪儿？'}]}
            with patch.object(server,'make_client',side_effect=model),patch.object(server,'update_scene_state') as scene,patch.object(server.long_memory,'refresh_summary'):
                result=client.post('/api/stream',json=body)
                self.assertIn('"done": true',result.text)
                self.assertEqual([m for m,_ in calls],['model-wan','model-yan','model-liu'])
                self.assertIn('小婉：周六一起看电影。',json.dumps(calls[1][1],ensure_ascii=False))
                # The stream only queues maintenance; execute it explicitly for this unit test.
                from backend.app.infrastructure import maintenance
                from sqlalchemy import select
                from sqlalchemy.orm import Session
                with Session(self.store.engine) as db:
                    jobs=list(db.scalars(select(maintenance.Job.id).where(maintenance.Job.kind=='scene')))
                self.assertEqual(len(jobs),1)
                maintenance.process(jobs[0])
                self.assertEqual(scene.call_count,1)
                client.post('/api/stream',json=body)
                self.assertEqual(len(calls),3)
            messages=client.get('/api/messages').json()['messages']
            self.assertEqual(len(messages),4)
            self.assertEqual([m['speaker'] for m in messages[1:]],list(actors.ROLES.values()))
            self.assertEqual(len(client.get('/api/memory/export').json()['recent_log']),4)
            self.assertEqual(client.post('/api/save',json={'messages':[{'role':m['role'],'content':m['content']} for m in messages]}).json()['saved'],0)

    def test_failure_keeps_completed_messages_and_releases_lease(self):
        async def broken(**kwargs):
            raise RuntimeError('offline')
        fake=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
        with TestClient(server.app) as client,patch.object(server,'load_config',return_value={'api_key':'fake','model':'test','temperature':.2}),patch.object(server,'make_client',return_value=fake):
            result=client.post('/api/stream',json={'request_id':'failed-turn','messages':[{'role':'user','content':'小研你好'}]})
            self.assertIn('"error"',result.text)
            self.assertEqual(len(client.get('/api/messages').json()['messages']),1)
            self.assertTrue(chat_history.claim('next-turn'))
            chat_history.release('next-turn')

    def test_browser_import_and_bootstrap_do_not_duplicate(self):
        storage.append_log([{'who':'他','text':'旧对话'},{'who':'小研','text':'旧回复'}])
        with TestClient(server.app) as client:
            self.assertEqual(len(client.get('/api/messages').json()['messages']),2)
            payload={'messages':[{'role':'user','content':'新消息'}]}
            self.assertEqual(client.post('/api/messages/import',json=payload).json()['imported'],1)
            self.assertEqual(client.post('/api/messages/import',json=payload).json()['imported'],0)
            self.assertEqual(len(client.get('/api/messages').json()['messages']),3)
            self.assertEqual(len(storage.memory_pack()['recent_log']),3)

    def test_partial_group_failure_keeps_first_reply(self):
        calls=[]
        async def create(**kwargs):
            calls.append(kwargs)
            if len(calls)>1:
                raise RuntimeError('second actor offline')
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='小婉：我的回复已经完成。'))])
            return chunks()
        fake=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with TestClient(server.app) as client,patch.object(server,'load_config',return_value={'api_key':'fake','model':'test','temperature':.2}),patch.object(server,'make_client',return_value=fake):
            response=client.post('/api/stream',json={'request_id':'partial','messages':[{'role':'user','content':'你们各自回答'}]})
            self.assertIn('"error"',response.text)
            messages=client.get('/api/messages').json()['messages']
            self.assertEqual(len(messages),2)
            self.assertEqual(messages[-1]['speaker'],'小婉')
            self.assertEqual(len(client.get('/api/memory/export').json()['recent_log']),2)
            self.assertTrue(chat_history.claim('after-partial'))
            chat_history.release('after-partial')

    def test_window_lease_prevents_overlapping_generations(self):
        self.assertTrue(chat_history.claim('first'))
        with self.assertRaises(RuntimeError):
            chat_history.claim('second')
        chat_history.release('first')
        self.assertTrue(chat_history.claim('second'))
        chat_history.release('second')

    def test_clear_display_survives_reentry_preserves_memory_and_new_messages(self):
        with TestClient(server.app) as client:
            chat_history.bootstrap()
            chat_history.append('old',0,'user','不能删除的记忆')
            self.assertEqual(len(client.get('/api/messages').json()['messages']),1)
            self.assertEqual(client.post('/api/messages/clear-display').status_code,200)
            self.assertEqual(client.get('/api/messages').json()['messages'],[])
            self.assertEqual(client.get('/api/messages').json()['messages'],[])
            self.assertEqual(len(chat_history.messages()),1)
            self.assertEqual(client.get('/api/conversations').json()[0]['activity']['group'],{})
            self.assertEqual(storage.memory_pack()['recent_log'][0]['text'],'不能删除的记忆')
            chat_history.append('new',0,'user','清空后的消息')
            messages=client.get('/api/messages').json()['messages']
            self.assertEqual([m['content'] for m in messages],['清空后的消息'])
            activity=client.get('/api/conversations').json()[0]['activity']['group']
            self.assertEqual(activity['preview'],'清空后的消息')
            self.assertGreater(activity['updated_at'],0)
            self.assertEqual(client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages'],[])

    def test_database_copy_includes_messages(self):
        from migrate_to_postgres import copy_rows
        chat_history.bootstrap()
        chat_history.append('migrate',0,'user','迁移消息')
        target=storage.Store('sqlite:///'+(Path(self.tmp.name)/'destination.db').as_posix())
        try:
            self.assertEqual(copy_rows(Path(self.tmp.name)/'chat.db',target),1)
            with patch.object(storage,'_STORE',target):
                self.assertEqual(chat_history.messages()[-1]['content'],'迁移消息')
        finally:
            target.engine.dispose()

    def test_previews_unread_read_cursor_and_isolation(self):
        chat_history.bootstrap()
        chat_history.append('preview',0,'user','你好')
        chat_history.append('preview',1,'assistant','小研：下午好','小研')
        with TestClient(server.app) as client:
            activity=client.get('/api/conversations').json()[0]['activity']['group']
            self.assertEqual(activity['preview'],'小研：下午好')
            self.assertEqual(activity['unread'],1)
            last=client.get('/api/messages').json()['messages'][-1]['id']
            self.assertEqual(client.post('/api/messages/read',json={'message_id':last}).status_code,200)
            self.assertEqual(client.get('/api/conversations').json()[0]['activity']['group']['unread'],0)
            self.assertEqual(client.post('/api/messages/read',headers={'X-Chat-Channel':'wan'},json={'message_id':last}).status_code,422)

    def test_read_cursor_keeps_preview_and_only_counts_new_replies(self):
        chat_history.bootstrap()
        chat_history.append('read-one',0,'user','你好')
        chat_history.append('read-one',1,'assistant','小研：第一条回复','小研')
        first=chat_history.messages()[-1]
        chat_history.append('read-two',0,'assistant','小研：第二条回复','小研')
        second=chat_history.messages()[-1]
        with TestClient(server.app) as client:
            self.assertEqual(client.get('/api/conversations').json()[0]['activity']['group']['unread'],2)
            client.post('/api/messages/read',json={'message_id':first['id']})
            self.assertEqual(client.get('/api/conversations').json()[0]['activity']['group']['unread'],1)
            client.post('/api/messages/read',json={'message_id':second['id']})
            client.post('/api/messages/read',json={'message_id':first['id']})
            activity=client.get('/api/conversations').json()[0]['activity']['group']
            self.assertEqual(activity['unread'],0)
            self.assertEqual(activity['preview'],'小研：第二条回复')
            chat_history.append('read-three',0,'assistant','小研：新消息','小研')
            activity=client.get('/api/conversations').json()[0]['activity']['group']
            self.assertEqual(activity['unread'],1)
            self.assertEqual(activity['incoming_at'],activity['updated_at'])

    def test_private_state_updates_world_without_sharing_private_context(self):
        from unittest.mock import MagicMock
        state={'时间':'下午','地点':'厨房','在场人物':'小研','小婉':'未知','小研':'已到厨房，正在喝水','小柳':'未知','主要交谈角色':'小研','上一段停在':'私密约定'}
        model=MagicMock()
        model.__enter__.return_value=model
        def extract(**kwargs):
            source=json.loads(kwargs['messages'][1]['content'])['messages'][0]['source_id']
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'events':[{
                'source_id':source,'actor':'yan','evidence':'走进厨房喝水','status':'厨房·喝水',
                'position':{'place':'厨房'}}],'summary':'私密约定'},ensure_ascii=False)))])
        model.chat.completions.create.side_effect=extract
        world=server.chat_world.set({'id':'default','channel':'yan','data':self.store.read('owner','default')})
        try:
            with actors.role_scope('owner','default','yan'),patch('openai.OpenAI',return_value=model):
                server.update_scene_state([{'role':'user','content':'你到厨房了吗？'},{'role':'assistant','content':'小研：（走进厨房喝水）已经到了。'}],cfg_override={'api_key':'fake','model':'test','base_url':''})
        finally:
            server.chat_world.reset(world)
        public=self.store.read('owner','default')
        self.assertEqual(public['role_states']['yan']['status'],'厨房·喝水')
        self.assertNotIn('私密约定',json.dumps(public,ensure_ascii=False))
        _,payload=self._payload('wan',[{'role':'user','content':'你好'}])
        self.assertNotIn('厨房·喝水',json.dumps(payload,ensure_ascii=False))
        _,payload=self._payload('yan',[{'role':'user','content':'你好'}])
        self.assertIn('厨房·喝水',json.dumps(payload,ensure_ascii=False))


    def test_all_windows_share_live_status_ignore_legacy_private_scenes(self):
        self.store.mutate('owner', 'default', lambda data: data.update(
            scene_state={'时间':'下午','地点':'客厅','小研':'在客厅'},
            role_states={'yan':{'status':'正在厨房喝水','source':'private'}}))
        with actors.role_scope('owner','default','yan'):
            storage.write_document('scene_state', {'时间':'昨天','小研':'在公园'})
        with TestClient(server.app) as client:
            states=[client.get('/api/info',headers={'X-Chat-Channel':channel}).json()['scene_state']
                    for channel in ['group','wan','yan','liu']]
            self.assertTrue(all(state==states[0] for state in states))
            self.assertEqual(states[0]['小研'],'正在厨房喝水')
            exported=client.get('/api/memory/export',headers={'X-Chat-Channel':'yan'}).json()
            self.assertEqual(exported['scene_state'],states[0])
        token=server.chat_world.set({'id':'default','channel':'yan','data':{}})
        try:
            with actors.role_scope('owner','default','yan'):
                _,dynamic=server.build_prompt_parts()
                self.assertIn('正在厨房喝水',dynamic)
                self.assertNotIn('在公园',dynamic)
                self.store.mutate('owner','default',lambda data:data['role_states']['yan'].update(status='已回卧室'))
                self.assertEqual(server.displayed_scene()['小研'],'已回卧室')
        finally:
            server.chat_world.reset(token)

    def test_group_scene_refresh_preserves_unchanged_private_state(self):
        from unittest.mock import MagicMock
        self.store.mutate('owner','default',lambda data:data.update(
            scene_state={'时间':'下午','地点':'客厅','小研':'在客厅','小婉':'坐着'},
            role_states={'yan':{'status':'厨房喝水','source':'private'}}))
        state={'时间':'下午','地点':'客厅','小研':'厨房喝水','小婉':'站起身'}
        model=MagicMock();model.__enter__.return_value=model
        def extract(**kwargs):
            source=json.loads(kwargs['messages'][1]['content'])['messages'][0]['source_id']
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'events':[{
                'source_id':source,'actor':'wan','evidence':'小婉站起身','status':'站起身'}]},ensure_ascii=False)))])
        model.chat.completions.create.side_effect=extract
        token=server.chat_world.set({'id':'default','channel':'group','data':{}})
        try:
            with patch('openai.OpenAI',return_value=model):
                server.update_scene_state([{'role':'user','content':'小婉站起身'}],cfg_override={'api_key':'fake','model':'test','base_url':''})
            public=self.store.read('owner','default')
            self.assertEqual(public['role_states']['yan']['status'],'厨房喝水')
            self.assertEqual(public['scene_state']['小研'],'在客厅')
            self.assertEqual(server.displayed_scene()['小婉'],'站起身')
        finally:
            server.chat_world.reset(token)


    def test_one_person_timeline_links_private_request_and_group_response(self):
        with actors.role_scope('owner','default','wan'):
            chat_history.append('private-request',0,'user','去群里发个1')
        chat_history.append('public-post',0,'assistant','小婉：1','小婉')
        chat_history.append('public-question',0,'user','发1干嘛')
        for role in ['wan','yan']:
            with actors.role_scope('owner','default',role):
                storage.append_log([{'who':'他','text':'only-secret-'+role}])
        _,payload=self._payload('wan',[{'role':'user','content':'你怎么不说话了'}])
        timeline=next(m['content'] for m in payload if '以下按时间排序' in m['content'])
        self.assertLess(timeline.index('去群里发个1'),timeline.index('小婉：1'))
        self.assertLess(timeline.index('小婉：1'),timeline.index('发1干嘛'))
        self.assertNotIn('only-secret-yan',json.dumps(payload,ensure_ascii=False))
        _,other=self._payload('yan',[{'role':'user','content':'你好'}])
        self.assertNotIn('去群里发个1',json.dumps(other,ensure_ascii=False))

    def test_group_each_role_can_speak_or_remain_silent(self):
        calls=[]
        replies={'wan':actors.SILENCE,'yan':'小研：我想去植物园。','liu':actors.SILENCE}
        def model(cfg):
            async def create(**kwargs):
                role=cfg['model'];calls.append((role,kwargs['messages']))
                async def chunks():
                    for text in [replies[role][:4],replies[role][4:]]:
                        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])
                return chunks()
            async def close(): pass
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
        with TestClient(server.app) as client:
            for role in actors.ROLES:
                client.post('/api/settings',headers={'X-Chat-Channel':role},json={'model':role,'api_key':'fake'})
            with patch.object(server,'make_client',side_effect=model),patch.object(server,'update_scene_state'),patch.object(server.long_memory,'refresh_summary'):
                result=client.post('/api/stream',json={'request_id':'mixed','messages':[{'role':'user','content':'周末呢'}]})
                messages=client.get('/api/messages').json()['messages']
                self.assertEqual([m['speaker'] for m in messages if m['role']=='assistant'],['小研'])
                self.assertNotIn(actors.SILENCE,result.text)
                self.assertIn('小研：我想去植物园。',json.dumps(calls[-1][1],ensure_ascii=False))
                replies.update({role:actors.SILENCE for role in actors.ROLES})
                result=client.post('/api/stream',json={'request_id':'silent','messages':[{'role':'user','content':'不用回答，我记下了'}]})
                self.assertIn('"no_reply": true',result.text)
                self.assertNotIn(actors.SILENCE,result.text)
                self.assertEqual(len(client.get('/api/messages').json()['messages']),3)
                count=len(calls)
                client.post('/api/stream',json={'request_id':'silent','messages':[{'role':'user','content':'不用回答，我记下了'}]})
                self.assertEqual(len(calls),count)


    def test_private_read_failure_retries_and_clears_only_its_badge(self):
        for role in ['yan','wan']:
            with actors.role_scope('owner','default',role):
                chat_history.bootstrap()
                chat_history.append('unread-'+role,0,'assistant',actors.ROLES[role]+'：你好',actors.ROLES[role])
        with TestClient(server.app) as client:
            headers={'X-Chat-Channel':'yan'}
            latest=client.get('/api/messages',headers=headers).json()['messages'][-1]['id']
            with patch.object(chat_history,'mark_read',side_effect=RuntimeError('retry')):
                self.assertEqual(client.post('/api/messages/read',headers=headers,json={'message_id':latest}).status_code,409)
            self.assertEqual(client.get('/api/conversations').json()[0]['activity']['yan']['unread'],1)
            self.assertEqual(client.post('/api/messages/read',headers=headers,json={'message_id':latest}).status_code,200)
            activity=client.get('/api/conversations').json()[0]['activity']
            self.assertEqual(activity['yan']['unread'],0)
            self.assertEqual(activity['wan']['unread'],1)
            self.assertTrue(activity['yan']['preview'])


    def test_all_four_channels_clear_reentry_and_reject_old_browser_reimport(self):
        from backend.app.runtime import role_actions
        with TestClient(server.app) as client:
            for channel in ['group','wan','yan','liu']:
                with self.subTest(channel=channel),role_actions.window('owner','default',channel):
                    chat_history.bootstrap()
                    chat_history.append('clear-old-'+channel,0,'assistant','旧消息','小研')
                    original=chat_history.messages()
                    memory=storage.memory_pack()['recent_log']
                    headers={'X-Chat-Channel':channel}
                    result=client.post('/api/messages/clear-display',headers=headers)
                    self.assertEqual(result.status_code,200)
                    self.assertGreater(result.json()['display_cleared_at'],0)
                    for _ in range(2):self.assertEqual(client.get('/api/messages',headers=headers).json()['messages'],[])
                    activity=client.get('/api/conversations').json()[0]['activity'][channel]
                    self.assertEqual(activity,{})
                    # Another tab with an old cache must not recreate old messages with fresh times.
                    imported=client.post('/api/messages/import',headers=headers,json={'messages':[{'role':'assistant','content':'缓存旧消息'}]})
                    self.assertEqual(imported.json()['imported'],0)
                    self.assertEqual(imported.json()['messages'],[])
                    self.assertEqual(chat_history.messages(),original)
                    self.assertEqual(storage.memory_pack()['recent_log'],memory)
                    chat_history.append('clear-new-'+channel,0,'assistant','清除后真正的新消息','小研')
                    messages=client.get('/api/messages',headers=headers).json()['messages']
                    self.assertEqual([m['content'] for m in messages],['清除后真正的新消息'])
                    self.assertEqual(client.get('/api/conversations').json()[0]['activity'][channel]['unread'],1)

    def test_clear_handles_clock_skew_and_new_messages_still_appear(self):
        with TestClient(server.app) as client:
            chat_history.bootstrap()
            with patch('backend.app.conversations.chat_history.time.time',return_value=2000000000):
                chat_history.append('future-old',0,'assistant','旧消息','小研')
            with patch('backend.app.conversations.chat_history.time.time',return_value=1900000000):
                self.assertEqual(client.post('/api/messages/clear-display').status_code,200)
                self.assertEqual(client.get('/api/messages').json()['messages'],[])
                chat_history.append('future-new',0,'assistant','新消息','小研')
            messages=client.get('/api/messages').json()['messages']
            self.assertEqual([m['content'] for m in messages],['新消息'])


    def test_coordinator_order_is_followed_and_later_role_reads_saved_reply(self):
        from unittest.mock import AsyncMock
        calls=[]
        def model(cfg):
            async def create(**kwargs):
                name=next(name for name in actors.ROLES.values() if '你只扮演'+name in kwargs['messages'][-1]['content'])
                calls.append((name,kwargs['messages']))
                if name=='小婉':
                    # Previous role's actual message has already been committed, not a draft.
                    self.assertTrue(any(message['speaker']=='小研' for message in chat_history.messages()))
                    self.assertIn('小研：先去植物园。',json.dumps(kwargs['messages'],ensure_ascii=False))
                async def chunks():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=name+'：'+('先去植物园。' if name=='小研' else '那我也同意。')))])
                return chunks()
            async def close():pass
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
        with TestClient(server.app) as client,patch.object(server.group_coordinator,'plan',new=AsyncMock(side_effect=[['yan','wan'],['wan'],[]])) as planner,patch.object(server,'make_client',side_effect=model):
            for role in actors.ROLES:client.post('/api/settings',headers={'X-Chat-Channel':role},json={'model':'test','api_key':'fake'})
            response=client.post('/api/stream',json={'request_id':'nominated-order','messages':[{'role':'user','content':'周末安排呢？'}]})
            self.assertIn('"done": true',response.text)
            self.assertEqual([name for name,_ in calls],['小研','小婉'])
            client.post('/api/stream',json={'request_id':'nominated-order','messages':[{'role':'user','content':'周末安排呢？'}]})
            self.assertEqual(planner.call_count,3)

    def test_independent_memory_preferences_and_public_traits_settings(self):
        with TestClient(server.app) as client:
            headers={'X-Chat-Channel':'yan'}
            response=client.post('/api/settings',headers=headers,json={'public_persona':'公开话少','memory_preferences':{'group_public':.6,'private_public':.2}})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['public_persona'],'公开话少')
            self.assertEqual(response.json()['memory_preferences']['private_public'],.2)
            self.assertNotEqual(client.get('/api/settings',headers={'X-Chat-Channel':'wan'}).json()['memory_preferences']['private_public'],.2)
            for prefs in [{'group_public':.99},{'unknown':.5}]:
                self.assertEqual(client.post('/api/settings',headers=headers,json={'memory_preferences':prefs}).status_code,422)
            self.assertEqual(client.get('/api/settings',headers=headers).json()['memory_preferences']['group_public'],.6)
