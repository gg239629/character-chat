"""Backed-up morning reset and short, scoped physical statuses."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from sqlalchemy import select
from sqlalchemy.orm import Session
import actors
import chat_history
import maintenance
import role_actions
import scene_labels
import server
import storage
from story_reset import reset_story


class FreshStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=storage.Store('sqlite:///'+(self.root/'test.db').as_posix())
        self.store.ensure('owner','default',{})
        self.patch=patch.object(storage,'_STORE',self.store);self.patch.start()

    def tearDown(self):
        self.patch.stop();self.store.engine.dispose();self.tmp.cleanup()

    def test_reset_backs_up_all_memories_preserves_settings_and_isolates_other_story(self):
        self.store.ensure('owner','other-story',{'recent_log':[{'who':'他','text':'保留的另一个故事'}]})
        for channel in ['group',*actors.ROLES]:
            with role_actions.window('owner','default',channel):
                chat_history.bootstrap();chat_history.append('before',0,'user','旧记忆')
                storage.write_document('long_summaries',[{'text':'旧归档'}])
                storage.write_document('important_facts',[{'text':'旧事实'}])
                storage.write_document('persona','基础性格')
                storage.write_document('settings',{'model':'configured'})
                maintenance.enqueue('owner','default',channel,'before',time.time())
        result=reset_story('owner','default',self.root/'backups')
        backup=json.loads(Path(result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(len(backup['tables']['chat_messages']),4)
        self.assertEqual(len(backup['tables']['chat_maintenance_jobs']),8)
        for channel in ['group',*actors.ROLES]:
            with role_actions.window('owner','default',channel):
                self.assertEqual(chat_history.messages(),[])
                for field in ['recent_log','pending_archive','long_summaries','important_facts','original_backups']:
                    self.assertEqual(storage.read_document(field,[]),[])
                self.assertEqual(storage.read_document('persona',''),'基础性格')
                self.assertEqual(storage.read_document('settings',{})['model'],'configured')
                self.assertEqual(chat_history.import_browser([{'role':'user','content':'旧缓存'}]),0)
                self.assertTrue(all(len(server.displayed_scene()[name])<=10 for name in actors.ROLES.values()))
        self.assertEqual(self.store.read('owner','other-story')['recent_log'][0]['text'],'保留的另一个故事')
        with Session(self.store.engine) as db:self.assertEqual(list(db.scalars(select(maintenance.Job))),[])

    def test_private_status_changes_only_owner_and_group_can_change_multiple(self):
        reset_story('owner','default',self.root/'backups')
        response={'地点':'厨房','小研':{'地点':'厨房','动作':'煮粥'},'小婉':{'地点':'院子','动作':'跑步'},'小柳':{'地点':'书房','动作':'看书'}}
        fake=MagicMock();fake.__enter__.return_value=fake
        fake.chat.completions.create.side_effect=lambda **kwargs:SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response,ensure_ascii=False)))])
        cfg={'api_key':'fake','model':'test','base_url':''}
        with patch('openai.OpenAI',return_value=fake):
            with role_actions.window('owner','default','yan'):
                server.update_scene_state([{'role':'assistant','content':'小研：（走进厨房煮粥）早饭喝粥'}],cfg_override=cfg)
                scene=server.displayed_scene()
                self.assertEqual(scene['小研'],'厨房·煮粥')
                self.assertEqual(scene['小婉'],'卧室·刚起床');self.assertEqual(scene['小柳'],'卧室·刚起床')
                self.assertEqual(scene['地点'],'各自住处')
            with role_actions.window('owner','default','group'):
                server.update_scene_state([{'role':'user','content':'小婉在院子跑步，小柳在书房看书'}],cfg_override=cfg)
                scene=server.displayed_scene()
                self.assertEqual(scene['小婉'],'院子·跑步');self.assertEqual(scene['小柳'],'书房·看书')
                self.assertTrue(all(len(scene[name])<=10 for name in actors.ROLES.values()))

    def test_reject_long_labels_preserve_old_and_remove_static_old_history(self):
        self.assertEqual(scene_labels.label({'地点':'厨房','动作':'煮粥'}),'厨房·煮粥')
        self.assertEqual(scene_labels.label({'地点':'一个很长很长的地点','动作':'和别人说完昨天的事情'},'卧室·起床'),'卧室·起床')
        profile=('### 2.3 性格底色\n基础人设\n### 2.5 性欲与体质\n体质设定\n'
            '### 5.2 小研走过的路\n旧秘密\n## 5. 共同记忆\n旧历史\n## 8. 现成开场\n旧开场\n### 4.12 事后\n另一个人设')
        filtered=scene_labels.fresh_profile(profile)
        self.assertIn('基础人设',filtered);self.assertIn('另一个人设',filtered)
        self.assertIn('体质设定',filtered)
        self.assertNotIn('旧秘密',filtered);self.assertNotIn('旧历史',filtered);self.assertNotIn('旧开场',filtered)

    def test_backup_failure_aborts_and_pre_reset_action_cannot_send(self):
        chat_history.bootstrap();chat_history.append('before',0,'user','旧消息')
        with patch.object(Path,'open',side_effect=OSError('backup unavailable')):
            with self.assertRaises(OSError):reset_story('owner','default',self.root/'backups')
        self.assertEqual(len(chat_history.messages()),1)
        old=role_actions.insert_action('owner','default','wan','group','autonomous','before',{'decision':'send','message':'不能再发的旧消息'},now=time.time()-60)
        self.store.mutate('owner','default',lambda data:data.update(memory_reset_at=time.time()))
        result=asyncio.run(role_actions.deliver(old['id']))
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(len(chat_history.messages()),1)
