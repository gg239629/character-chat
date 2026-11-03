"""Transport-independent character API shared by HTTP, MCP and background work."""
from contextlib import contextmanager
from backend.app.infrastructure import storage
from backend.app.core import context
from backend.app.characters import actors
from backend.app.conversations import message_service, chat_inbox, chat_history
from backend.app.world import realtime_world


class CharacterService:
    @contextmanager
    def scope(self,user,world,channel):
        data=storage.get_store().read(user,world)
        if data.get('world_id'):raise PermissionError('A private channel is not a world')
        data=realtime_world.sync(user,world,data)
        cid=message_service.conversation(user,world,channel)
        scope=storage.scope.set((user,cid));snapshot=storage.request_snapshot.set(storage.load_request_data(user,cid))
        token=context.chat_world.set({'id':world,'channel':channel,'data':data})
        try:yield cid
        finally:
            context.chat_world.reset(token);storage.request_snapshot.reset(snapshot);storage.scope.reset(scope)

    def send(self,user,world,channel,text,request_id):
        from backend.app.core.schemas import ChatIn
        request=ChatIn(request_id=request_id,messages=[{'role':'user','content':text}])
        with self.scope(user,world,channel):
            result=chat_inbox.accept(request)
            return {**result,'message_id':chat_history.identity(storage.scope.get()[1],request_id,0)}

    def run(self,user,world,channel,identity):
        with self.scope(user,world,channel):return chat_inbox.state(identity)

    def messages(self,user,world,channel,limit=50):
        with self.scope(user,world,channel):return chat_history.visible_messages()[-max(1,min(limit,100)):]


characters=CharacterService()
