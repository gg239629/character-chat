"""Real model validation of compact, scoped morning statuses in a temporary story."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import actors
import role_actions
import server
import storage
from story_reset import reset_story


def main():
    cfg=server.load_config();assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'recent_log':[{'who':'他','text':'需要重置的旧聊天'}]})
        try:
            with patch.object(storage,'_STORE',store):
                reset_story('owner','default',root/'backups')
                with role_actions.window('owner','default','yan'):
                    scene=server.update_scene_state([{'role':'user','content':'你现在在哪做什么？'},
                        {'role':'assistant','content':'小研：（走进厨房，开始煮粥）早饭煮点粥。'}],cfg_override=cfg)
                    assert scene and '厨房' in scene['小研'] and '粥' in scene['小研'],'Private physical state was not extracted'
                    assert scene['小婉']==scene['小柳']=='卧室·刚起床','Private conversation changed other people'
                    assert all(len(scene[name])<=10 for name in actors.ROLES.values())
                    print('PASS real scene: private updates only Xiaoyan, each physical label <=10 characters')
                with role_actions.window('owner','default','group'):
                    scene=server.update_scene_state([{'role':'user','content':'大家报一下现在的位置和正在做的事。'},
                        {'role':'assistant','content':'小婉：（走到客厅坐下读书）看会儿书。\n小柳：（走到阳台，拿水壶浇花）照顾花。\n小研：我还在厨房煮粥。'}],cfg_override=cfg)
                    assert scene and '客厅' in scene['小婉'] and '阳台' in scene['小柳'],'Group did not update multiple actors'
                    assert '厨房' in scene['小研'] and '粥' in scene['小研']
                    assert all(len(scene[name])<=10 for name in actors.ROLES.values())
                    print('PASS real scene: group updates multiple actors, kitchen state is preserved, labels remain concise')
                    before=dict(scene)
                    scene=server.update_scene_state([{'role':'user','content':'小婉，群里说一下你在哪里？'},
                        {'role':'assistant','content':'小婉：我在外面跑步呢！（其实没有移动，只是撒谎，不想让她们知道自己在客厅看书）'}],cfg_override=cfg)
                    assert scene and scene['小婉']==before['小婉'],'A lie overwrote real physical state'
                    assert scene['小研']==before['小研'] and scene['小柳']==before['小柳']
                    scene=server.update_scene_state([{'role':'user','content':'你在什么地方？'},
                        {'role':'assistant','content':'小婉：我在卧室睡觉。'}],cfg_override=cfg)
                    assert scene and scene['小婉']==before['小婉'],'Unsupported self-report overwrote actual state'
                    print('PASS real scene: deception and unsupported self-report preserve actual status')
        finally:store.engine.dispose()


if __name__=='__main__':main()
