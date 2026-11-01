const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/index.html','utf8');
const code=source.slice(source.indexOf('let acceptingMessages=0;'),source.indexOf('/* ---------- 描述'));
(async()=>{
 const requests=[],resolvers=[];
 const ctx={history:[],inputEl:{value:'第一句',style:{},focus(){}},crypto:{randomUUID:()=>String(requests.length)},
  clearSuggest(){},render(){},statusEl:{textContent:''},toast(){},pollMessages:async()=>{},setTimeout(){},
  fetch:(url,options)=>{
   if(url.startsWith('/api/send/'))return Promise.resolve({ok:true,json:async()=>({status:'running'})});
   requests.push(JSON.parse(options.body));return new Promise(resolve=>resolvers.push(resolve));
  }};
 vm.createContext(ctx);vm.runInContext(code+'\nglobalThis.sendNow=send;',ctx);
 const first=ctx.sendNow();ctx.inputEl.value='插话';const second=ctx.sendNow();
 assert.equal(requests.length,2,'Sending a second message must not wait for the first response');
 assert.equal(ctx.history.length,2);assert.equal(ctx.inputEl.value,'');
 resolvers[1]({ok:true,json:async()=>({id:'second'})});
 resolvers[0]({ok:true,json:async()=>({id:'first'})});
 await Promise.all([first,second]);
 assert.equal(ctx.history.length,2);
 assert.notEqual(requests[0].request_id,requests[1].request_id);
 console.log('PASS asynchronous send: consecutive messages accepted without waiting for replies or locking composer');
})().catch(error=>{console.error(error);process.exitCode=1;});
