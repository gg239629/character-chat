"""Neutral real-model JSON protocol check; no actual role messages are sent."""
import asyncio,sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server; from backend.app.infrastructure import storage; from backend.app.runtime import role_actions; from backend.app.infrastructure import model_usage

async def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        store=storage.Store('sqlite:///'+(Path(tmp)/'test.db').as_posix());store.ensure('owner','default',{})
        try:
            with patch.object(storage,'_STORE',store),model_usage.scope('peer_followup'):
                result=await role_actions.ask_json(cfg,
                    '根据实际收到的日常问答，决定是否补充告诉用户。只输出JSON对象：decision(send/none)、message。简短自然，不添加事实。',
                    {'question':'面做好了吗？','actual_answer':'面已经做好，可以吃了。'})
                assert result.get('decision') in ('send','none'),'Invalid followup decision'
                if result['decision']=='send':assert isinstance(result.get('message'),str) and result['message'].strip()
                print('PASS: actual model returned a valid peer_followup JSON decision; no chat messages sent')
        finally:store.engine.dispose()

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as error:
        print('FAIL:',type(error).__name__)
        if isinstance(error,AssertionError):print(str(error))
        raise SystemExit(1)
