const fs=require('fs'),vm=require('vm'),assert=require('assert');
(async()=>{
 let date='2026-11-01T10:00:00',opened=0,active=false,listener,dialog,scope='a';
 class TestDate extends Date{constructor(...args){super(...(args.length?args:[date]));}}
 const values=new Map();
 const ctx={Date:TestDate,window:{},location:{assign(){}},fetch:async()=>({ok:true,json:async()=>({tutorial_scope:scope})}),
  localStorage:{getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)},
  document:{hidden:false,querySelector:()=>active?dialog:null,addEventListener:(type,fn)=>listener=fn,
   body:{append(){}},createElement:()=>{const buttons={};dialog={style:{},setAttribute(){},showModal(){opened++;active=true},close(){active=false},querySelector:id=>buttons[id]||(buttons[id]={})};return dialog;}}};
 vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/onboarding.js','utf8'),ctx);
 const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};await flush();
 assert.equal(opened,1);dialog.close();listener();await flush();assert.equal(opened,1);
 date='2026-11-02T00:01:00';listener();await flush();assert.equal(opened,2);
 dialog.close();scope='b';listener();await flush();assert.equal(opened,3);
 console.log('PASS tutorial: one display per local day and account, repeats only next day');
})().catch(e=>{console.error(e);process.exitCode=1;});
