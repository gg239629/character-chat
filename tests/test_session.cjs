const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/session.js','utf8');
function store(initial={}){const value={...initial};return {value,getItem:k=>value[k]??null,setItem:(k,v)=>value[k]=v,removeItem:k=>delete value[k]};}
async function setup(identity={enabled:true,logged_in:true,scope:'alice',username:'alice'}){
 const events={},calls=[],redirects=[],sessionStorage=store(),localStorage=store();
 const location={origin:'https://chat.example',href:'https://chat.example/chat?channel=yan',pathname:'/chat',search:'?channel=yan',replace:url=>redirects.push(url),reload:()=>redirects.push('reload')};
 const input={value:'尚未发送的草稿'},document={documentElement:{style:{}},getElementById:()=>input,body:{replaceChildren(){},append(){}},createElement:()=>({style:{}})};
 let status=200,unsubscribed=0;
 const window={addEventListener:(name,fn)=>events[name]=fn,fetch:async(url,options={})=>{
  calls.push({url,options});if(url==='/api/auth/status')return {ok:true,json:async()=>identity};
  return {ok:status===200,status,clone(){return {json:async()=>status===409?{code:'account_changed'}:{}};}};
 }};
 const navigator={serviceWorker:{getRegistration:async()=>({pushManager:{getSubscription:async()=>({endpoint:'https://push.example/device',unsubscribe:async()=>{unsubscribed++;return true;}})},getNotifications:async()=>[]})}};
 vm.runInNewContext(source,{window,document,location,navigator,sessionStorage,localStorage,URL,Headers,AbortSignal,Date});
 await window.chatSessionReady;
 return {window,document,location,events,calls,redirects,sessionStorage,localStorage,input,setStatus:n=>status=n,unsubscribed:()=>unsubscribed};
}
(async()=>{
 let app=await setup();assert.equal(app.window.chatCachePrefix,'account:alice:');
 await app.window.fetch('/api/messages');assert.equal(app.calls.at(-1).options.headers.get('X-Session-Scope'),'alice');
 app.setStatus(401);await app.window.fetch('/api/messages');assert(app.redirects[0].startsWith('/login?next='));
 assert.equal(app.sessionStorage.getItem('chat-draft:alice:/chat?channel=yan'),'尚未发送的草稿');
 app=await setup();app.input.value='';app.setStatus(401);await app.window.fetch('/api/send',{method:'POST',body:JSON.stringify({messages:[{role:'user',content:'刚点发送但登录过期'}]})});
 assert.equal(app.sessionStorage.getItem('chat-draft:alice:/chat?channel=yan'),'刚点发送但登录过期');
 app=await setup();app.setStatus(409);await app.window.fetch('/api/messages');assert.equal(app.redirects[0],'/');assert.equal(Object.keys(app.sessionStorage.value).length,0);
 app=await setup();app.events.storage({key:'chat-account-changed'});assert.equal(app.redirects[0],'/');
 app=await setup({enabled:true,logged_in:false,scope:''});assert(app.redirects[0].startsWith('/login?next='));
 app=await setup({enabled:false,logged_in:true,scope:'personal'});assert.equal(app.window.chatCachePrefix,'');assert.equal(app.redirects.length,0);
 app=await setup();app.localStorage.setItem('account:alice:companion-chat-v1','private');app.localStorage.setItem('account:bob:companion-chat-v1','other');
 // Storage keys in browsers are enumerable. Mirror that aspect for the logout test.
 for(const key of Object.keys(app.localStorage.value))app.localStorage[key]=app.localStorage.value[key];
 await app.window.chatLogout();assert.equal(app.unsubscribed(),1);assert.equal(app.localStorage.getItem('account:alice:companion-chat-v1'),null);assert.equal(app.localStorage.getItem('account:bob:companion-chat-v1'),'other');
 assert.equal(JSON.parse(app.calls.at(-1).options.body).endpoint,'https://push.example/device');assert.equal(app.redirects.at(-1),'/login');
 console.log('PASS sessions: bootstrap, scoped headers/cache, expired drafts, account change, personal mode, device logout');
})().catch(error=>{console.error(error);process.exitCode=1;});
