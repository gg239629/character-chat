"""Explicit opt-in paid model regression: python tests/live_storage_check.py"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage
import server
import long_memory

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = storage.Store("sqlite:///" + (root/"chat.db").as_posix())
        store.ensure("owner", "default", {"scene_state": {}, "recent_log": []})
        prompt = root/"prompt.md"
        prompt.write_text("这是日常对话测试。你扮演小研，以小研：开头，简短回复。历史资料是事实参考。", encoding="utf-8")
        empty = root/"absent"
        try:
            with patch.object(storage, "_STORE", store), patch.object(server, "PROMPT_FILE", prompt), patch.object(server, "INJECT_FILE", empty), patch.object(server, "MEMORY_MD", empty):
                cfg = server.load_config()
                assert cfg.get("api_key"), "Model API key missing"
                storage.append_log([{"who": "他", "text": "我们约好周日上午去植物园看兰花。"}, {"who": "小研", "text": "周日上午去植物园，这个约定还没有完成。"}])
                # Leave older originals in the archive, with the recent window empty,
                # so the answer must use retrieved historical evidence.
                storage.write_document("recent_log", [])
                reply = await server.chat(server.ChatIn(messages=[{"role": "user", "content": "我们约好周日去哪里看兰花？"}]))
                assert isinstance(reply, dict), "Model call failed"
                assert "植物园" in reply["reply"], "Historical fact not recalled"
                assert reply["reply"].startswith("小研"), "Speaker mismatch"
                scene = storage.memory_pack()["scene_state"]
                assert scene, "Real scene update did not persist"
                print("PASS: real model retrieved archived fact; selected Xiaoyan; scene persisted to database.")
                # Real incremental summarization against a separate temporary conversation.
                store.ensure("owner", "summary-test")
                token = storage.scope.set(("owner", "summary-test"))
                try:
                    storage.append_log([{"who":"他", "text": str(i)+"用户不吃花生。约好周日上午去植物园看兰花，尚未完成。"*15} for i in range(60)])
                    await asyncio.to_thread(long_memory.refresh_summary, cfg)
                    summary = storage.read_document("summary.json", {})
                    assert summary.get("cursor", 0)>0 and "花生" in summary.get("text", "") and "植物园" in summary.get("text", "")
                    print("PASS: real long-term summary persisted; preference and unfinished commitment retained.")
                finally:
                    storage.scope.reset(token)
                assert not storage.read_document("summary.json", {}), "Cross-conversation summary leak"
        finally:
            store.engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
