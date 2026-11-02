const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/index.html','utf8').replace(/\r\n/g,'\n');
const receipts=source.slice(source.indexOf('let latestMessage = null;'),source.indexOf("document.addEventListener('visibilitychange',()=>{if(!document.hidden)markVisibleRead();});"));
const poll=source.slice(source.indexOf('let pollingMessages=false;'),source.indexOf('setInterval(pollMessages,1500);'));
const clear=source.slice(source.indexOf('document.getElementById("clear").addEventListener("click", async()=>{'),source.indexOf('sendEl.disabled=true;describeEl.disabled=true;\nload()'));
const load=source.slice(source.indexOf('async function load(recoverBrowser=true){'),source.indexOf('/* ---------- 渲染 ---------- */'));
(async()=>{
 for(const channel of ['group','wan','yan','liu']){
  let clearHandler,resolvePoll,afterClear=false,imports=0;
  const button={disabled:false,addEventListener:(name,fn)=>{clearHandler=fn;}};
  const values=new Map(),errors=[];
  const ctx={AbortSignal,STORE_KEY:'cache:'+channel,SAVED_KEY:'saved:'+channel,history:[],savedCount:0,busy:false,acceptingMessages:0,
   document:{hidden:false,getElementById:()=>button},chatEl:{scrollTop:100,scrollHeight:500,clientHeight:400},
   localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
   render(){},confirm:()=>true,toast:(text,type)=>{if(type==='error')errors.push(text);},
   statusEl:{textContent:''},dlg:{close(){}},inputEl:{focus(){}},Date,
   fetch:async url=>{
    if(url==='/api/messages/clear-display'){afterClear=true;return {ok:true,json:async()=>({ok:true,display_cleared_at:123,messages:[]})};}
    if(url==='/api/messages/import'){imports++;throw Error('must not restore old cache');}
    if(url==='/api/messages/read')return {ok:true};
    if(afterClear)return {ok:true,json:async()=>({messages:[],display_cleared_at:123})};
    return new Promise(resolve=>{resolvePoll=resolve;});
   }};
  vm.createContext(ctx);
  vm.runInContext(receipts+'\n'+poll+'\n'+load+'\n'+clear+'\nglobalThis.api={pollMessages,load,receiveMessages};',ctx);
  const old=[{id:'old-'+channel,role:'assistant',content:'old cached message'}];
  ctx.api.receiveMessages(old);
  const pending=ctx.api.pollMessages();
  await clearHandler();
  resolvePoll({ok:true,json:async()=>({messages:old})});await pending;
  assert.equal(ctx.history.length,0,channel+': in-flight GET resurrected old history');
  assert.equal(values.has(ctx.STORE_KEY),false);
  assert.equal(values.get(ctx.STORE_KEY+':db-migrated'),'true');
  values.set(ctx.STORE_KEY,JSON.stringify(old));values.set(ctx.SAVED_KEY,'0');values.delete(ctx.STORE_KEY+':db-migrated');
  await ctx.api.load();
  assert.equal(imports,0,channel+': reentry reimported old browser cache');
  assert.equal(ctx.history.length,0);assert.deepEqual(errors,[]);
 }
 console.log('PASS all four windows: clear rejects stale polls, reentry ignores legacy cache, migration marker remains cleared');
})().catch(error=>{console.error(error);process.exitCode=1});
