self.addEventListener('push',event=>{
  let message;
  try{message=event.data.json();}catch{return;}
  event.waitUntil((async()=>{
    const url=new URL(message.url,self.location.origin);
    if(url.origin!==self.location.origin || url.pathname!=='/chat')return;
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const visible=windows.find(client=>{
      const current=new URL(client.url);
      return client.visibilityState==='visible' && current.pathname==='/chat' &&
        (current.searchParams.get('conversation')||'default')===url.searchParams.get('conversation') &&
        (current.searchParams.get('channel')||'group')===url.searchParams.get('channel');
    });
    if(visible){visible.postMessage({type:'new-message'});return;}
    await self.registration.showNotification(message.title||'新消息',{
      body:message.body||'你收到了一条新消息',tag:message.tag,data:{url:url.href}});
  })());
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil((async()=>{
    const url=new URL(event.notification.data?.url||'/',self.location.origin);
    if(url.origin!==self.location.origin)return;
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const found=windows.find(client=>client.url===url.href);
    if(found){await found.focus();return;}
    await self.clients.openWindow(url.href);
  })());
});
