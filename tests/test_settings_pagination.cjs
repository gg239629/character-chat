const fs=require('fs'),vm=require('vm'),assert=require('assert');
const window={};
vm.runInNewContext(fs.readFileSync('static/settings-layout.js','utf8'),{window,document:{getElementById:()=>null}});
const paginate=window.settingsPagination;
// Every visible field appears exactly once, including after a resize or async content change.
for(const available of [100,160,280,500]){
  for(const heights of [[30,0,50,80,40],[60,90,75,80,0,40],[0,0],[]]){
    const pages=paginate(heights,available),all=Array.from(pages.flat());
    assert.deepEqual(all,heights.flatMap((h,i)=>h>0?[i]:[]));
    for(const page of pages)assert(page.reduce((sum,i)=>sum+heights[i],0)<=available);
  }
}
assert.equal(paginate([50,50],100).length,1);
assert.equal(paginate([50,51],100).length,2);
const source=fs.readFileSync('static/index.html','utf8');
const toggle={checked:true},fields=Array.from({length:5},()=>({disabled:false}));
const context={document:{getElementById:()=>toggle},settingsLoading:false};
['sBase','sModel','sKey','sTemp','sTokens'].forEach((name,i)=>context[name]=fields[i]);
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function updateModelInheritance(){'),source.indexOf("document.getElementById('inheritModel').onchange")),context);
context.updateModelInheritance();assert(fields.every(field=>field.disabled));
toggle.checked=false;context.updateModelInheritance();assert(fields.every(field=>!field.disabled));
context.settingsLoading=true;context.updateModelInheritance();assert(fields.every(field=>field.disabled));
console.log('PASS settings: pagination preserves fields, height budgets, inherited model locks every connection field');
