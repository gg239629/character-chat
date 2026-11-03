import { reactive } from 'vue';
import type { Auth,Channel } from './types';
export const auth=reactive<Auth>({enabled:false,logged_in:false,username:'',scope:'',registration_mode:'closed'});
let initialized=false;
export async function bootstrap(force=false){if(initialized&&!force)return;const r=await fetch('/api/auth/status',{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error('无法读取账号状态，请刷新重试');Object.assign(auth,await r.json());initialized=true;}
export interface Scope {world:string;channel:Channel}
export async function api<T>(path:string,options:RequestInit={},scope?:Scope):Promise<T>{
 const headers=new Headers(options.headers);if(options.body)headers.set('Content-Type','application/json');
 if(auth.enabled)headers.set('X-Session-Scope',auth.scope);
 if(scope){headers.set('X-Conversation-ID',scope.world);headers.set('X-Chat-Channel',scope.channel);}
 const response=await fetch(path,{...options,headers,cache:'no-store',signal:options.signal||AbortSignal.timeout(20000)});
 const data=await response.json();
 if(!response.ok){
  if(!path.startsWith('/api/auth/')&&(response.status===401||data.code==='account_changed')){
   window.dispatchEvent(new CustomEvent('auth-expired',{detail:{changed:data.code==='account_changed',body:options.body}}));
   location.assign(data.code==='account_changed'?'/':'/login?next='+encodeURIComponent(location.pathname+location.search));
  }
  throw Error(typeof data.detail==='string'?data.detail:data.error||'请求失败，请稍后重试');
 }
 return data as T;
}
export const json=(value:unknown)=>JSON.stringify(value);
export async function logout(){
 let endpoint='';
 if('serviceWorker' in navigator){const reg=await navigator.serviceWorker.getRegistration('/');const sub=await reg?.pushManager?.getSubscription();endpoint=sub?.endpoint||'';if(sub)await sub.unsubscribe();(await reg?.getNotifications())?.forEach(n=>n.close());}
 await api('/api/auth/logout',{method:'POST',body:json({endpoint})});
 localStorage.removeItem('companion-web-push');localStorage.removeItem('companion-notifications-disable-pending');localStorage.setItem('companion-notifications-disabled','true');
 for(const key of Object.keys(localStorage))if(key.startsWith('account:'+auth.scope+':'))localStorage.removeItem(key);
 for(const key of Object.keys(sessionStorage))if(key.startsWith('chat-draft:'+auth.scope+':'))sessionStorage.removeItem(key);
 localStorage.setItem('chat-account-changed',String(Date.now()));location.assign('/login');
}
window.addEventListener('storage',event=>{if(event.key==='chat-account-changed')location.assign('/');});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
