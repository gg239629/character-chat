const fs=require('fs'),vm=require('vm'),assert=require('assert');
(async()=>{
 const nodes=new Map(),calls=[],redirects=[];
 function el(id){if(!nodes.has(id))nodes.set(id,{value:'',type:'password',attrs:{},focus(){this.focused=true;},setAttribute(k,v){this.attrs[k]=v;}});return nodes.get(id);}
 let failure=false,pending;
 const context={document:{getElementById:el},navigator:{},URL,URLSearchParams,AbortSignal,Date,
  location:{origin:'https://chat.example',search:'?next=https://outside.example/',replace:url=>redirects.push(url)},
  localStorage:{setItem(){},removeItem(){}},
  fetch:async(url,options)=>{
   calls.push({url,options});
   if(url==='/api/auth/status')return {ok:true,json:async()=>({enabled:true,logged_in:false,registration_mode:'invite'})};
   if(pending)await pending;
   return {ok:!failure,json:async()=>failure?{detail:'邀请码无效'}:{ok:true}};
  }};
 vm.runInNewContext(fs.readFileSync('static/login.js','utf8'),context);
 await new Promise(setImmediate);
 assert.equal(el('authSubmit').disabled,false);el('registerTab').onclick();
 assert.equal(el('confirmPassword').required,true);assert.equal(el('inviteCode').required,true);
 el('username').value='alice';el('password').value='example-password-123';el('confirmPassword').value='different';
 await el('authForm').onsubmit({preventDefault(){}});assert.match(el('authStatus').textContent,/不一致/);assert.equal(calls.length,1);
 el('confirmPassword').value=el('password').value;el('inviteCode').value='invite-code';failure=true;
 await el('authForm').onsubmit({preventDefault(){}});assert.equal(el('authStatus').textContent,'邀请码无效');assert.equal(el('authSubmit').disabled,false);
 failure=false;await el('authForm').onsubmit({preventDefault(){}});assert.match(el('authStatus').textContent,/注册成功/);assert.equal(el('password').value,'');assert.equal(el('username').value,'alice');assert.equal(el('confirmPassword').required,false);
 el('showPassword').onclick();assert.equal(el('password').type,'text');assert.equal(el('showPassword').attrs['aria-pressed'],'true');
 let resolve;pending=new Promise(r=>resolve=r);el('password').value='example-password-123';
 const request=el('authForm').onsubmit({preventDefault(){}});const count=calls.length;
 await el('authForm').onsubmit({preventDefault(){}});assert.equal(calls.length,count,'double submit must not create two login requests');
 resolve();await request;assert.equal(redirects.at(-1),'/','external return URLs must not redirect off-site');
 console.log('PASS login UI: modes, invitation, confirmation, error recovery, password visibility, duplicate guard, safe redirect');
})().catch(error=>{console.error(error);process.exitCode=1;});
