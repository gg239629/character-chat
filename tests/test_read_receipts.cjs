const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/index.html','utf8');
const receipts=source.slice(source.indexOf('let latestMessage = null;'),source.indexOf("document.addEventListener('visibilitychange',()=>{if(!document.hidden)markVisibleRead();});"));
const polling=source.slice(source.indexOf('let pollingMessages=false;'),source.indexOf('setInterval(pollMessages,1500);'));
let attempts=0,getFailed=false,readFailure=true;
let messages=[{id:'yan-one',role:'assistant',content:'小研：你好'}];
const notices=[];
const context={document:{hidden:false},chatEl:{scrollHeight:500,clientHeight:400,scrollTop:100},
 localStorage:{removeItem(){},setItem:(key,value)=>notices.push(key)},STORE_KEY:'test',history:[],savedCount:0,busy:false,acceptingMessages:0,
 statusEl:{textContent:''},render(){},persist(){},Date,
 fetch:async url=>{
  if(url==='/api/messages/read'){attempts++;if(readFailure){readFailure=false;return {ok:false};}return {ok:true};}
  if(getFailed)throw Error('temporary connection failure');
  return {ok:true,json:async()=>({messages})};
 }};
vm.createContext(context);
vm.runInContext(receipts+'\n'+polling+'\nglobalThis.api={receiveMessages,markVisibleRead,pollMessages,lastRead:()=>lastReadId};',context);
(async()=>{
 const api=context.api;
 api.receiveMessages(messages);
 await api.markVisibleRead();assert.equal(attempts,1);assert.equal(api.lastRead(),'');
 await api.pollMessages();assert.equal(attempts,2);assert.equal(api.lastRead(),'yan-one');
 await api.pollMessages();assert.equal(attempts,2,'unchanged successful receipt must deduplicate');
 assert(notices.includes('companion-read-changed'));
 messages=[{id:'yan-two',role:'assistant',content:'小研：新消息'}];api.receiveMessages(messages);
 context.document.hidden=true;await api.markVisibleRead();await api.pollMessages();assert.equal(attempts,2);
 context.document.hidden=false;context.chatEl.scrollTop=0;context.chatEl.scrollHeight=900;
 await api.pollMessages();assert.equal(attempts,2,'reading old history must not clear new unread');
 context.chatEl.scrollTop=500;getFailed=true;
 await api.pollMessages();assert.equal(attempts,3,'retry the visible receipt even if polling GET fails');
 assert.equal(api.lastRead(),'yan-two');
 api.receiveMessages([]);await api.markVisibleRead();assert.equal(attempts,3,'empty chat must forget stale latest ID');
 api.receiveMessages([{id:'first',role:'user',content:'第一条'}]);
 api.receiveMessages([{id:'second',role:'assistant',content:'第二条'}],true);
 api.receiveMessages([{id:'second',role:'assistant',content:'第二条'}],true);
 assert.equal(context.history.length,2,'delta merge must preserve originals and deduplicate retries');
 api.receiveMessages([]);api.receiveMessages([{id:'fresh',role:'user',content:'清除后'}],true);
 assert.equal(context.history.length,1,'reset must discard old delta cache');
 const homeSource=fs.readFileSync('static/home.html','utf8');
 const homeFunction=homeSource.slice(homeSource.indexOf('async function loadStories(){'),homeSource.indexOf('/* Account controls are initialized'));
 let resolveFirst,homeCalls=0;
 const firstResponse=new Promise(resolve=>{resolveFirst=resolve;});
 const homeContext={AbortSignal,list:{replaceChildren(){},children:[]},status:{textContent:''},document:{title:''},
  fetch:async()=>{homeCalls++;return homeCalls===1?firstResponse:{ok:true,json:async()=>[]};}};
 vm.createContext(homeContext);
 vm.runInContext('const notified=new Map();let initialized=false,loading=false,refreshAgain=false,storySignature="";'+homeFunction+';globalThis.load=loadStories;',homeContext);
 const pending=homeContext.load();await homeContext.load();
 resolveFirst({ok:true,json:async()=>[]});await pending;
 for(let i=0;i<5;i++)await Promise.resolve();
 assert.equal(homeCalls,2,'returning home/read update during a pending fetch must schedule a fresh load');
 console.log('PASS read receipts: failed POST retries without new messages, deduplication, hidden/history protection, failed poll recovery, empty reset');
})().catch(error=>{console.error(error);process.exitCode=1});
