"""Real model regression for a single person's cross-window experience and optional speech."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import actors
import chat_history
import server
import storage


def turn(client, channel, identity, text):
    response=client.post('/api/stream',headers={'X-Chat-Channel':channel},json={
        'request_id':identity,'messages':[{'role':'user','content':text}]})
    assert response.status_code==200
    result=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert not any(e.get('error') for e in result),'Model request failed'
    assert any(e.get('done') for e in result),'Missing completion'
    return next(e for e in result if e.get('done'))


def main():
    cfg=server.load_config()
    assert not server.check_cfg(cfg),'Model configuration missing'
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=storage.Store('sqlite:///'+(root/'test.db').as_posix())
        store.ensure('owner','default',{'scene_state':{'时间':'下午','地点':'各自家中'},'recent_log':[]})
        config=root/'model.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        prompt=root/'daily.md';prompt.write_text('日常微信聊天。人物是独立成年人，正常短句聊天，不代替别人发言。',encoding='utf-8')
        try:
            with patch.object(storage,'_STORE',store),patch.object(server,'CONFIG_FILE',config),patch.object(server,'PROMPT_FILE',prompt),patch.object(server,'MEMORY_MD',root/'absent'),patch.dict('os.environ',{'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),'RUN_ROLE_SCHEDULER':'false'}),TestClient(server.app) as client:
                for role in actors.ROLES:
                    persona='愿意帮忙，记得自己做过什么，被人故意逗会直接指出，简短自然。' if role=='wan' else '话少，不喜欢插话。用户与别人交流时只默读；没有被问到就不发言。'
                    assert client.post('/api/settings',headers={'X-Chat-Channel':role},json={'persona':persona,'public_persona':'喜欢帮忙，乐意接话' if role=='wan' else '话少，没有直接问自己就默读','max_tokens':250}).status_code==200
                turn(client,'wan','request-one','请现在在群里只发一个数字1，没有隐私，你可以直接发。')
                group=client.get('/api/messages').json()['messages']
                assert any(m['speaker']=='小婉' and '1' in m['content'] for m in group),'Role did not send requested number'
                done=turn(client,'group','why-one','小婉，你发1干嘛？')
                latest=[m for m in done['messages'] if m['role']=='assistant' and m['id']!=group[-1]['id']]
                assert latest and latest[0]['speaker']=='小婉','Addressed actor did not react'
                assert all(m['speaker']=='小婉' for m in latest),'Quiet roles unnecessarily interrupted'
                print('PASS real group: same actor connects private request to public reaction; other roles remain silent')
                done=turn(client,'wan','recall-one','我刚让你发完又在群里问你发1干嘛，你怎么看我这番操作？')
                reply=done['messages'][-1]['content']
                assert any(word in reply for word in ['逗','耍','刁难','故意','装','明知','捉弄','玩','你让我','你叫我','让我发']), 'Cross-window reaction not understood: '+ascii(reply)
                print('PASS real private: recognizes the public reversal as teasing using the same personal experience')
                before=len(client.get('/api/messages').json()['messages'])
                turn(client,'group','quiet','这条只作备忘，不用回复，大家继续忙自己的吧。')
                after=client.get('/api/messages').json()['messages']
                assert len(after)==before+1,'Roles ignored explicit no-reply request'
                print('PASS real group: all three may remain silent; user message still saved')
        finally:
            store.engine.dispose()


if __name__=='__main__':main()
