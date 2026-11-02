/* Bootstrap identity before reading any browser chat cache or starting requests. */
(()=>{
 const original=window.fetch.bind(window);
 let identity=null,redirecting=false;
 const next=()=>location.pathname+location.search;
 function leave(changed=false,draft){
  if(redirecting)return;redirecting=true;
  document.documentElement.style.visibility='hidden';
  const input=document.getElementById('input');
  const text=draft??input?.value;
  if(!changed&&identity?.scope&&text)sessionStorage.setItem('chat-draft:'+identity.scope+':'+next(),text);
  location.replace(changed?'/':'/login?next='+encodeURIComponent(next()));
 }
 window.fetch=async(url,options={})=>{
  const target=new URL(typeof url==='string'?url:url.url,location.href);
  const api=target.origin===location.origin&&target.pathname.startsWith('/api/');
  if(api&&identity?.enabled&&!target.pathname.startsWith('/api/auth/')){
   const headers=new Headers(options.headers);headers.set('X-Session-Scope',identity.scope);options={...options,headers};
  }
  const response=await original(url,options);
  if(api&&!target.pathname.startsWith('/api/auth/')&&(response.status===401||response.status===409)){
   const data=await response.clone().json().catch(()=>({}));
   if(response.status===401||data.code==='account_changed'){
    let draft;
    if(response.status===401&&target.pathname==='/api/send'){
     try{const sent=JSON.parse(options.body).messages?.filter(m=>m.role==='user').map(m=>m.content).join('\n');const typed=document.getElementById('input')?.value;draft=[sent,typed].filter(Boolean).join('\n');}catch{}
    }
    leave(response.status===409,draft);
   }
  }
  return response;
 };
 window.chatSessionReady=(async()=>{
  document.documentElement.style.visibility='hidden';
  try{
   const response=await original('/api/auth/status',{cache:'no-store',signal:AbortSignal.timeout(15000)});
   if(!response.ok)throw Error();
   identity=await response.json();window.chatSession=identity;
   if(identity.enabled&&!identity.logged_in){leave();return false;}
   window.chatCachePrefix=identity.enabled?'account:'+identity.scope+':':'';
   document.documentElement.style.visibility='';return true;
  }catch{
   document.documentElement.style.visibility='';
   document.body.replaceChildren();const message=document.createElement('p');message.style.cssText='padding:32px;color:#ddd';message.textContent='暂时无法连接服务器，请刷新重试。';document.body.append(message);return false;
  }
 })();
 window.addEventListener('storage',event=>{if(event.key==='chat-account-changed')leave(true);});
 window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
 window.chatLogout=async()=>{
  let endpoint='';
  if('serviceWorker' in navigator){
   const registration=await navigator.serviceWorker.getRegistration('/');
   const subscription=await registration?.pushManager?.getSubscription();endpoint=subscription?.endpoint||'';
   if(subscription)await subscription.unsubscribe();
   const notices=await registration?.getNotifications?.();notices?.forEach(n=>n.close());
  }
  const response=await original('/api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint}),signal:AbortSignal.timeout(15000)});
  if(!response.ok)throw Error('退出失败，请重试');
  localStorage.removeItem('companion-web-push');localStorage.setItem('companion-notifications-disabled','true');
  localStorage.setItem('chat-account-changed',String(Date.now()));
  if(identity?.scope){
   const prefix='account:'+identity.scope+':';
   Object.keys(localStorage).filter(k=>k.startsWith(prefix)).forEach(k=>localStorage.removeItem(k));
   Object.keys(sessionStorage).filter(k=>k.startsWith('chat-draft:'+identity.scope+':')).forEach(k=>sessionStorage.removeItem(k));
  }
  location.replace('/login');
 };
})();
