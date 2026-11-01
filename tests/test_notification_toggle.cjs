const fs=require('fs'),vm=require('vm'),assert=require('assert');
(async()=>{
  const values=new Map(),events={};let serverOK=true,browserOK=true,unsubscribed=0,requests=0;
  const button={setAttribute(k,v){this[k]=v;}},status={};
  const subscription={endpoint:'https://push.example/device',unsubscribe:async()=>{unsubscribed++;return browserOK;}};
  const window={Notification:{permission:'granted',requestPermission:async()=> 'granted'},PushManager:{},addEventListener:(name,fn)=>events[name]=fn};
  const context={window,Notification:window.Notification,AbortSignal,
    navigator:{serviceWorker:{getRegistration:async()=>({pushManager:{getSubscription:async()=>subscription}})}},
    localStorage:{getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)},
    fetch:async()=>{requests++;return {ok:serverOK};}};
  vm.runInNewContext(fs.readFileSync('static/notifications.js','utf8'),context);
  const api=window.chatNotifications;api.bind(button,status);
  assert.equal(button.textContent,'关闭通知');
  // A legacy granted permission can be disabled; permission remains granted after reload.
  await button.onclick();assert.equal(button.textContent,'开启通知');assert.equal(api.enabled(),false);assert.equal(unsubscribed,1);
  api.bind(button,status);assert.equal(button.textContent,'开启通知');
  delete window.PushManager;await button.onclick();assert.equal(button.textContent,'关闭通知');assert.equal(api.enabled(),true);
  // Server failure still attempts browser unsubscribe and successfully turns notifications off.
  serverOK=false;await button.onclick();assert.equal(button.textContent,'开启通知');assert.equal(unsubscribed,2);
  await button.onclick();browserOK=false;serverOK=true;await button.onclick();assert.equal(button.textContent,'开启通知');
  // Both removal paths failing must leave an explicit retry action, never a false success.
  await button.onclick();serverOK=false;await button.onclick();assert.equal(button.textContent,'关闭通知');assert.match(status.textContent,/重试/);
  serverOK=true;await button.onclick();assert.equal(button.textContent,'开启通知');assert.equal(button.disabled,false);
  // State from another tab updates this tab's label.
  values.delete('companion-notifications-disabled');events.storage();assert.equal(button.textContent,'关闭通知');
  assert(requests>=5);
  console.log('PASS notifications: enable/disable, reload, independent removal, failure retry, cross-tab state');
})().catch(e=>{console.error(e);process.exitCode=1;});
