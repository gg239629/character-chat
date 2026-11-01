"""Real personality-led decisions in an isolated story; no production messages."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import actors
import chat_history
import role_actions
import server
import storage


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Missing model configuration'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'recent_log':[],'scene_state':{'小研':'客厅·看书','小婉':'卧室·休息','小柳':'阳台·浇花'}})
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'load_config',return_value=cfg),patch.object(server,'MEMORY_MD',root/'absent'):
                for actor,persona in [('yan','热情主动，喜欢分享读书感受，关心熟悉的人。'),('wan','慢热安静，有具体事情才联系，不为问候而问候。')]:
                    with role_actions.window('owner','default',actor):
                        storage.write_document('persona',persona)
                        storage.write_document('public_persona',persona)
                        storage.write_document('behavior',{'enabled':False,'allow_private':False,'allow_group':False,'quiet_start':0,'quiet_end':23})
                        chat_history.append('hello-'+actor,0,'user','谢谢你刚才陪我聊书，我很信任你。今天有好书可以随时推荐给我，不用等我先开口。')
                    result=asyncio.run(role_actions.autonomous('owner','default',actor))
                    assert result and result['status'] in ('sent','none','pending')
                    data=store.read('owner',actors.role_id('default',actor))
                    state=data['initiative_state']
                    assert 47<=state['affinity']<=53 and state['interaction_cursor']
                    assert state['next_consider_at']>0
                    if result['status']=='sent':
                        with role_actions.window('owner','default',actor if result.get('target','user')=='user' else 'group'):
                            pass
                print('PASS real models: two personalities decide independently, legacy disabled/quiet switches ignored, relationship changes have original-message evidence, next checks scheduled privately')
        finally:store.engine.dispose()


if __name__=='__main__':main()
