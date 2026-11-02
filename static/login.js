(()=>{
 const el=id=>document.getElementById(id),status=el('authStatus'),submit=el('authSubmit');
 let mode='login',registration='closed',busy=false;
 function destination(){const url=new URL(new URLSearchParams(location.search).get('next')||'/',location.origin);return url.origin===location.origin&&['/','/chat'].includes(url.pathname)?url.pathname+url.search:'/';}
 function setMode(value){
  if(busy||value==='register'&&registration==='closed')return;mode=value;status.textContent='';
  for(const [id,name] of [['loginTab','login'],['registerTab','register']]){el(id).setAttribute('aria-selected',String(name===mode));el(id).tabIndex=name===mode?0:-1;}
  el('authForm').setAttribute('aria-labelledby',mode==='login'?'loginTab':'registerTab');
  el('registerFields').hidden=mode==='login';el('confirmPassword').required=mode==='register';
  el('inviteField').hidden=registration!=='invite';el('inviteCode').required=mode==='register'&&registration==='invite';
  el('password').autocomplete=mode==='login'?'current-password':'new-password';el('password').minLength=mode==='register'?10:1;
  submit.textContent=mode==='login'?'登录':'创建账号';
 }
 el('loginTab').onclick=()=>setMode('login');el('registerTab').onclick=()=>setMode('register');
 for(const id of ['loginTab','registerTab'])el(id).onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();setMode(mode==='login'?'register':'login');el(mode==='login'?'loginTab':'registerTab').focus();}};
 el('showPassword').onclick=()=>{const show=el('password').type==='password';el('password').type=show?'text':'password';el('showPassword').textContent=show?'隐藏':'显示';el('showPassword').setAttribute('aria-pressed',String(show));el('showPassword').setAttribute('aria-label',show?'隐藏密码':'显示密码');};
 el('authForm').onsubmit=async event=>{
  event.preventDefault();if(busy)return;
  if(mode==='register'&&el('password').value!==el('confirmPassword').value){status.textContent='两次输入的密码不一致';el('confirmPassword').focus();return;}
  busy=true;submit.disabled=true;el('loginTab').disabled=el('registerTab').disabled=true;status.textContent=mode==='login'?'正在登录…':'正在创建账号…';
  try{
   let endpoint='';
   if(mode==='login'&&'serviceWorker' in navigator){
    const registration=await navigator.serviceWorker.getRegistration('/');
    const subscription=await registration?.pushManager?.getSubscription();endpoint=subscription?.endpoint||'';
    if(subscription && !await subscription.unsubscribe())throw Error('旧账号通知未能关闭，请稍后重试');
    const notices=await registration?.getNotifications?.();notices?.forEach(n=>n.close());
   }
   const response=await fetch('/api/auth/'+mode,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:el('username').value.trim(),password:el('password').value,invite_code:el('inviteCode').value.trim(),endpoint}),signal:AbortSignal.timeout(20000)});
   const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:data.error||'请检查填写内容后重试');
   if(mode==='register'){busy=false;setMode('login');el('password').value=el('confirmPassword').value='';el('inviteCode').value='';status.textContent='注册成功，请登录。';el('password').focus();}
   else{localStorage.removeItem('companion-web-push');localStorage.setItem('companion-notifications-disabled','true');localStorage.setItem('chat-account-changed',String(Date.now()));location.replace(destination());}
  }catch(error){status.textContent=error.name==='TimeoutError'?'连接超时，请稍后重试':error.message;}
  finally{busy=false;submit.disabled=false;el('loginTab').disabled=false;el('registerTab').disabled=registration==='closed';}
 };
 (async()=>{try{const r=await fetch('/api/auth/status',{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error('读取账号状态失败');const data=await r.json();if(!data.enabled||data.logged_in){location.replace(destination());return;}registration=data.registration_mode;el('registerTab').hidden=registration==='closed';submit.disabled=false;}catch(error){status.textContent='无法连接服务器，请刷新重试。';}})();
})();
