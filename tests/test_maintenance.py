"""Post-response work must not hold the chat lease or overwrite a newer scene."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
import actors
import chat_history
import maintenance as work
import role_actions
import server
import storage


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import AsyncMock
        import actors
        self.coordinator_patch=patch.object(server.group_coordinator,'plan',new=AsyncMock(side_effect=lambda messages,configs:[role for role in actors.candidates(messages) if not any(m.get('role')=='assistant' and m.get('content','').startswith(actors.ROLES[role]+'：') for m in server.recent_rounds(messages,1))]))
        self.coordinator_patch.start()
        self.addCleanup(self.coordinator_patch.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store('sqlite:///'+(Path(self.tmp.name)/'chat.db').as_posix())
        self.store.ensure('owner','default',{'scene_state':{'小婉':'在客厅'},'recent_log':[]})
        self.patches=[patch.object(storage,'_STORE',self.store),patch.dict('os.environ',{
            'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false','RUN_MAINTENANCE_WORKER':'false'}),
            patch.object(server,'load_config',return_value={'api_key':'fake','model':'test','base_url':'','temperature':.2})]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.store.engine.dispose();self.tmp.cleanup()

    def jobs(self):
        with Session(self.store.engine) as db:return list(db.scalars(select(work.Job).order_by(work.Job.through)))

    def seed(self,channel='group',source='turn',through=None):
        with role_actions.window('owner','default',channel):
            chat_history.append(source,0,'user','你好')
            through=through or chat_history.messages(1)[-1]['created_at']
        work.enqueue('owner','default',channel,source,through)
        return next(job for job in self.jobs() if job.kind=='scene' and job.channel==channel)

    def test_enqueue_is_idempotent_and_scopes_worker(self):
        scene=self.seed('yan')
        work.enqueue('owner','default','yan','turn',scene.through)
        self.assertEqual(len(self.jobs()),2)
        seen=[]
        def update_scene(history,cfg_override):
            seen.append((storage.scope.get(),server.chat_world.get()['channel'],history))
            return {}
        with patch.object(server,'update_scene_state',side_effect=update_scene):work.process(scene.id)
        self.assertEqual(seen[0][0],('owner',actors.role_id('default','yan')))
        self.assertEqual(seen[0][1],'yan')
        self.assertEqual(seen[0][2][-1]['content'],'你好')
        self.assertEqual(storage.scope.get(),('owner','default'))
        self.assertEqual(next(j for j in self.jobs() if j.id==scene.id).status,'done')
        with patch.object(server,'update_scene_state') as update_scene:
            work.process(scene.id);update_scene.assert_not_called()

    def test_cross_window_scene_jobs_serialize_and_recover(self):
        first=self.seed('wan','first')
        second=self.seed('yan','second')
        self.assertIsNone(work.reserve(second.id))
        lane,token=work.reserve(first.id)
        self.assertIsNone(work.reserve(first.id))
        self.assertIsNone(work.reserve(second.id))
        # A process crash leaves recoverable leases in the persistent DB.
        with Session(self.store.engine) as db:
            db.execute(update(work.Job).where(work.Job.id==first.id).values(lease_until=0))
            db.execute(update(work.Lane).where(work.Lane.id==lane).values(expires=0));db.commit()
        with patch.object(server,'update_scene_state',return_value={}):
            work.process(first.id);work.process(second.id)
        scenes=[j for j in self.jobs() if j.kind=='scene']
        self.assertTrue(all(j.status=='done' for j in scenes))
        self.assertEqual(scenes[0].attempts,2)

    def test_failure_retries_without_touching_chat_or_blocking_archive(self):
        scene=self.seed()
        with patch.object(server,'update_scene_state',return_value=None):work.process(scene.id)
        row=next(j for j in self.jobs() if j.id==scene.id)
        self.assertEqual(row.status,'pending');self.assertEqual(row.attempts,1)
        archive=next(j for j in self.jobs() if j.kind=='archive')
        with Session(self.store.engine) as db:
            db.execute(update(work.Job).where(work.Job.id==archive.id).values(due_at=0));db.commit()
        work.process(archive.id)
        self.assertEqual(next(j for j in self.jobs() if j.id==archive.id).status,'done')
        self.assertEqual(len(chat_history.messages()),1)
        self.assertTrue(chat_history.claim('next'));chat_history.release('next')
        with Session(self.store.engine) as db:
            db.execute(update(work.Job).where(work.Job.id==scene.id).values(attempts=2,due_at=0));db.commit()
        with patch.object(server,'update_scene_state',side_effect=ValueError('provider error')):work.process(scene.id)
        self.assertEqual(next(j for j in self.jobs() if j.id==scene.id).status,'failed')

    def test_stale_scene_commit_cannot_replace_newer_scene(self):
        self.store.mutate('owner','default',lambda data:data.update(scene_applied_at=200))
        fake=MagicMock();fake.__enter__.return_value=fake
        fake.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'小婉':'过时状态'})))])
        with role_actions.window('owner','default','group'):
            revision=server.scene_revision.set(100)
            try:
                with patch('openai.OpenAI',return_value=fake):
                    server.update_scene_state([{'role':'user','content':'旧轮次'}])
            finally:server.scene_revision.reset(revision)
        self.assertEqual(self.store.read('owner','default')['scene_state']['小婉'],'在客厅')

    def test_chat_returns_and_next_turn_works_while_scene_is_blocked(self):
        entered=threading.Event();release=threading.Event()
        def slow_scene(*args,**kwargs):
            entered.set();release.wait(10);return {}
        async def create(**kwargs):
            async def chunks():yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='小婉：你好。'))])
            return chunks()
        async def close():pass
        model=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=close)
        with patch.dict('os.environ',{'RUN_MAINTENANCE_WORKER':'true'}),patch.object(server,'make_client',return_value=model),patch.object(server,'update_scene_state',side_effect=slow_scene):
            with TestClient(server.app) as client:
                try:
                    body={'request_id':'fast-one','messages':[{'role':'user','content':'你好'}]}
                    response=client.post('/api/stream',headers={'X-Chat-Channel':'wan'},json=body)
                    self.assertIn('"done": true',response.text)
                    self.assertIn('"maintenance_pending": true',response.text)
                    self.assertTrue(entered.wait(2),'Background job did not start')
                    self.assertFalse(release.is_set())
                    body['request_id']='fast-two'
                    second=client.post('/api/stream',headers={'X-Chat-Channel':'wan'},json=body)
                    self.assertIn('"done": true',second.text)
                    self.assertFalse(release.is_set(),'Second reply waited for scene work')
                    self.assertEqual(len(client.get('/api/messages',headers={'X-Chat-Channel':'wan'}).json()['messages']),4)
                finally:release.set()


    def test_archive_lane_does_not_wait_for_scene_lane(self):
        scene=self.seed()
        lane,token=work.reserve(scene.id)
        archive=next(job for job in self.jobs() if job.kind=='archive')
        with Session(self.store.engine) as db:
            db.execute(update(work.Job).where(work.Job.id==archive.id).values(due_at=0));db.commit()
        with patch.object(server,'update_scene_state') as scene_call:
            work.process(archive.id)
            scene_call.assert_not_called()
        self.assertEqual(next(job for job in self.jobs() if job.kind=='archive').status,'done')
        work.finish(scene.id,lane,token,True)

    def test_database_migration_keeps_pending_work(self):
        from migrate_to_postgres import copy_rows
        self.seed()
        destination=storage.Store('sqlite:///'+(Path(self.tmp.name)/'copy.db').as_posix())
        try:
            copy_rows(Path(self.tmp.name)/'chat.db',destination)
            with Session(destination.engine) as db:
                jobs=list(db.scalars(select(work.Job)))
                self.assertEqual(len(jobs),2)
                self.assertTrue(all(job.status=='pending' for job in jobs))
        finally:destination.engine.dispose()


    def test_scene_does_not_borrow_actor_when_group_model_is_unconfigured(self):
        scene=self.seed()
        def config():
            return {'api_key':'fake','model':'actor-test','base_url':''} if storage.scope.get()[1]==actors.role_id('default','yan') else {}
        with patch.object(server,'load_config',side_effect=config),patch.object(server,'update_scene_state',return_value={}) as update_scene:
            work.process(scene.id)
            update_scene.assert_not_called()
        self.assertEqual(next(job for job in self.jobs() if job.id==scene.id).status,'pending')
