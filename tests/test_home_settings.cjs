const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/home.html','utf8');
const code=source.slice(source.indexOf('const homeSettingsDialog='),source.lastIndexOf('</script>'));
(async()=>{
 const nodes=new Map(),values=new Map(),calls=[];
 function node(id){if(!nodes.has(id))nodes.set(id,{value:'default',textContent:'',disabled:false,hidden:false,attrs:{},children:[],
  showModal(){this.open=true},close(){this.open=false},replaceChildren(){this.children=[]},append(item){this.children.push(item);this.value=item.value},
  setAttribute(k,v){this.attrs[k]=v},getAttribute(k){return this.attrs[k]}});return nodes.get(id)}
 let enabled=true,resetCount=0,fail=false,loaded=0;
 const context={document:{getElementById:node,createElement:()=>({})},availableStories:[{id:'default',title:'原有故事'}],
  notified:new Map(),Date,loadStories:async()=>{loaded++},
  localStorage:{removeItem:k=>values.delete(k),setItem:(k,v)=>values.set(k,v)},
  fetch:async(url,options={})=>{
   calls.push([url,options]);if(fail)return {ok:false};
   if(url==='/api/home/reset'){resetCount++;assert.equal(JSON.parse(options.body).confirm,true);return {ok:true,json:async()=>({backup:null})}}
   if(options.method==='PUT')enabled=JSON.parse(options.body).initiative_enabled;
   return {ok:true,json:async()=>({initiative_enabled:enabled})};
  }};
 vm.createContext(context);vm.runInContext(code,context);
 await node('homeSettings').onclick();assert.equal(node('homeSettingsDialog').open,true);
 assert.equal(node('initiativeToggle').getAttribute('aria-checked'),'true');
 await node('initiativeToggle').onclick();assert.equal(enabled,false);
 fail=true;await node('initiativeToggle').onclick();assert.equal(node('initiativeToggle').getAttribute('aria-checked'),'false');fail=false;
 node('resetStory').onclick();assert.equal(resetCount,0,'Opening confirmation cannot delete history');
 node('cancelReset').onclick();assert.equal(resetCount,0);
 values.set('companion-chat-v1','old');values.set('companion-chat-v1:private:yan','old');
 await node('confirmReset').onclick();assert.equal(resetCount,1);assert.equal(loaded,1);
 assert.equal(values.has('companion-chat-v1'),false);assert.equal(values.has('companion-chat-v1:private:yan'),false);
 assert.equal(values.get('companion-chat-v1:private:yan:db-migrated'),'true');
 console.log('PASS home settings: persisted switch, save failure recovery, explicit reset confirmation, old cache removal');
})().catch(error=>{console.error(error);process.exitCode=1});
