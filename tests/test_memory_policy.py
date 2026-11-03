import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import patch
from backend.app.infrastructure import storage
from backend.app.memory import long_memory


def conversation(count):
    return [{"who":"他" if i%2==0 else "小研", "text":f"日常消息{i}"} for i in range(count)]

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=storage.Store("sqlite:///"+(Path(self.tmp.name)/"test.db").as_posix())
        self.store.ensure("owner","default",{})
        self.patch=patch.object(storage,"_STORE",self.store);self.patch.start()
    def tearDown(self):
        self.patch.stop();self.store.engine.dispose();self.tmp.cleanup()
    def test_recent_only_until_overflow(self):
        storage.append_log(conversation(200))
        self.assertEqual(len(storage.memory_pack()["recent_log"]),200)
        self.assertEqual(storage.read_document("pending_archive",[]),[])
        storage.append_log(conversation(2))
        self.assertEqual(len(storage.memory_pack()["recent_log"]),200)
        self.assertEqual(len(storage.read_document("pending_archive",[])),2)
    def test_overflow_uses_round_boundaries(self):
        storage.append_log(conversation(201))
        recent=storage.memory_pack()["recent_log"]
        self.assertEqual(recent[0]["who"],"他")
        self.assertEqual(len(recent),199)
        self.assertEqual(len(storage.read_document("pending_archive",[])),2)
    def test_batch_and_failure_preserve_originals(self):
        storage.append_log(conversation(250))
        pending=storage.read_document("pending_archive",[])
        self.assertEqual(len(long_memory.pick_batch(pending)),50)
        class Fake:
            def __init__(self,**kw):self.chat=S(completions=self)
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def create(self,**kw):return S(choices=[S(message=S(content="invalid"))])
        with patch("openai.OpenAI",Fake):
            self.assertFalse(long_memory.refresh_summary({"api_key":"test","model":"test"}))
        self.assertEqual(storage.read_document("pending_archive",[]),pending)
    def test_independent_summary_and_original_backup(self):
        storage.append_log(conversation(250))
        class Fake:
            def __init__(self,**kw):self.chat=S(completions=self)
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def create(self,**kw):return S(choices=[S(message=S(content=json.dumps({"summary":"讨论日常安排。","facts":["约好周日去植物园。"]},ensure_ascii=False)))])
        with patch("openai.OpenAI",Fake):
            self.assertTrue(long_memory.refresh_summary({"api_key":"test","model":"test"}))
        self.assertEqual(len(storage.read_document("original_backups",[])),50)
        self.assertEqual(storage.read_document("pending_archive",[]),[])
        self.assertEqual(len(storage.read_document("long_summaries",[])),1)
        label, records=long_memory.recall("周日去植物园")
        self.assertIn("久远",label);self.assertTrue(records)
    def test_recent_evidence_precedes_summary(self):
        storage.append_log([{"who":"他","text":"纠正：植物园改到周六去。"}])
        storage.write_document("long_summaries",[{"text":"植物园约好周日去。"}])
        label,records=long_memory.recall("植物园哪天去")
        self.assertEqual(label,"近期原文")
        self.assertIn("周六",records[0]["text"])
    def test_legacy_migration_preserves_backup(self):
        self.store.mutate("owner","default",lambda data:data.update({"recent_log":[{"who":"他","text":"new"}],"archive.json":[{"id":"old","who":"他","text":"old"},{"id":"new","who":"他","text":"new"}]}))
        storage.ensure_policy()
        self.assertEqual(len(storage.read_document("legacy_memory_backup",{})["archive"]),2)
        self.assertEqual([r["text"] for r in storage.read_document("pending_archive",[])],["old"])
        storage.ensure_policy()
        self.assertEqual(len(storage.read_document("pending_archive",[])),1)
