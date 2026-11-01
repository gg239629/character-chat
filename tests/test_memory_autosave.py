import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.orm import Session
import actors
import chat_history
import maintenance
import memory_autosave as autosave
import role_actions
import server
import storage
from story_reset import reset_story


class MemoryAutosaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=storage.Store('sqlite:///'+(self.root/'test.db').as_posix());self.store.ensure('owner','default',{'recent_log':[],'memory_policy':2,'messages_initialized':True})
        self.patch=patch.object(storage,'_STORE',self.store);self.patch.start()

    def tearDown(self):
        self.patch.stop();self.store.engine.dispose();self.tmp.cleanup()

    def test_minute_checkpoint_batches_and_latest_messages_remain_readable(self):
        with patch('chat_history.time.time',return_value=120.5):
            chat_history.append('first',0,'user','刚说的内容')
            chat_history.append('second',0,'assistant','小研：我记得','小研')
        raw=self.store.read('owner','default')
        self.assertEqual(raw['recent_log'],[]);self.assertEqual(len(autosave.pending('owner','default')),2)
        self.assertEqual(autosave.checkpoint_due('owner','default'),180)
        with Session(self.store.engine) as db:self.assertEqual(list(db.scalars(select(chat_history.Message))),[])
        self.assertEqual(len(chat_history.messages()),2)
        self.assertEqual(len(storage.memory_pack()['recent_log']),2)
        self.assertFalse(autosave.flush('owner','default',179.99))
        self.assertTrue(autosave.flush('owner','default',180))
        raw=self.store.read('owner','default')
        self.assertEqual(len(raw['recent_log']),2);self.assertEqual(raw['memory_inbox'],[])
        self.assertEqual(raw['memory_saved_at'],180)
        self.assertFalse(autosave.flush('owner','default',180))
        self.assertEqual(len(raw['public_history']),2)

    def test_graceful_checkpoint_survives_restart_and_concurrent_flush_is_idempotent(self):
        chat_history.append('durable',0,'user','恢复记忆')
        due=autosave.checkpoint_due('owner','default')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:autosave.flush('owner','default',due),range(2)))
        self.assertEqual(sum(results),1)
        reopened=storage.Store('sqlite:///'+(self.root/'test.db').as_posix())
        try:
            with patch.object(storage,'_STORE',reopened):
                self.assertEqual(len(chat_history.messages()),1)
                self.assertEqual(len(storage.memory_pack()['recent_log']),1)
        finally:reopened.engine.dispose()
        chat_history.append('shutdown',0,'assistant','正常关闭补存','小研')
        autosave.flush_due(force=True)
        with Session(self.store.engine) as db:self.assertEqual(len(list(db.scalars(select(chat_history.Message)))),2)

    def test_scope_metadata_clear_and_reset_are_preserved(self):
        with role_actions.window('owner','default','yan'):
            chat_history.bootstrap()
            chat_history.append('private',0,'assistant','小研：1','小研',True,{'source':'role_action','action_id':'linked'})
            raw=self.store.read('owner',actors.role_id('default','yan'))
            self.assertEqual(raw.get('recent_log',[]),[])
            chat_history.clear_display()
            self.assertEqual(chat_history.visible_messages(),[])
            autosave.flush_due(autosave.checkpoint_due('owner',actors.role_id('default','yan')))
            log=storage.read_document('recent_log',[])
            self.assertEqual(log[0]['channel'],'yan');self.assertEqual(log[0]['action_id'],'linked')
            self.assertEqual(log[0]['audience'],['user','yan'])
            self.assertEqual(chat_history.visible_messages(),[])
        self.assertEqual(self.store.read('owner','default')['recent_log'],[])
        chat_history.append('before-reset',0,'user','不能恢复的旧待保存')
        reset_story('owner','default',self.root/'backups')
        autosave.flush_due(9999999999)
        self.assertEqual(self.store.read('owner','default')['recent_log'],[])
        self.assertEqual(chat_history.messages(),[])

    def test_overflow_and_archive_checks_run_at_minute_boundary_and_are_coalesced(self):
        with patch('chat_history.time.time',return_value=120.5):
            for i in range(250):chat_history.append('batch-'+str(i),0,'user' if i%2==0 else 'assistant','消息'+str(i),'' if i%2==0 else '小研')
            for i in range(3):maintenance.enqueue('owner','default','group','round-'+str(i),120.5+i,scene=False)
        with Session(self.store.engine) as db:
            jobs=list(db.scalars(select(maintenance.Job)))
            self.assertEqual(len(jobs),1);self.assertEqual(jobs[0].due_at,180)
        self.assertEqual(self.store.read('owner','default')['recent_log'],[])
        autosave.flush_due(180)
        data=self.store.read('owner','default')
        self.assertEqual(len(data['recent_log']),200)
        self.assertEqual(len(data['pending_archive']),50)
        self.assertEqual(autosave.pending('owner','default'),[])
        self.assertEqual(data['pending_archive'][0]['channel'],'group')
