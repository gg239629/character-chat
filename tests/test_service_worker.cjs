const fs=require('fs'),vm=require('vm'),assert=require('assert');
const handlers={},shown=[],opened=[],posted=[];
let windows=[];
const self={location:{origin:'https://chat.example'},addEventListener:(name,fn)=>handlers[name]=fn,
 clients:{matchAll:async()=>windows,openWindow:async url=>opened.push(url)},
 registration:{showNotification:async(title,options)=>shown.push({title,options})}};
vm.runInNewContext(fs.readFileSync('static/sw.js','utf8'),{self,URL});
async function push(url){let promise;handlers.push({data:{json:()=>({url})},waitUntil:p=>promise=p});await promise;}
(async()=>{
 await push('https://outside.example/chat');assert.equal(shown.length,0);
 await push('/chat?conversation=default&channel=wan');assert.equal(shown.length,1);
 windows=[{url:'https://chat.example/chat?conversation=default&channel=wan',visibilityState:'visible',postMessage:m=>posted.push(m),focus:async()=>posted.push('focused')}];
 await push('/chat?conversation=default&channel=wan');assert.equal(shown.length,1);assert.equal(posted.length,1);
 windows[0].visibilityState='hidden';await push('/chat?conversation=default&channel=wan');assert.equal(shown.length,2);
 let promise;handlers.notificationclick({notification:{close(){},data:{url:'https://chat.example/chat?conversation=default&channel=wan'}},waitUntil:p=>promise=p});await promise;assert(posted.includes('focused'));
 handlers.notificationclick({notification:{close(){},data:{url:'https://outside.example/'}},waitUntil:p=>promise=p});await promise;assert.equal(opened.length,0);
 console.log('PASS service worker: hidden push, visible chat refresh, click focus, origin protection');
})().catch(error=>{console.error(error);process.exitCode=1});
