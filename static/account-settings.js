(async()=>{
 if(!await window.chatSessionReady)return;
 const el=id=>document.getElementById(id),dialog=el('accountDialog'),status=el('accountHint');
 const session=window.chatSession;
 const open=()=>{status.textContent=session.enabled?'当前账号：'+session.username:'当前为个人空间，尚未启用账号功能。';el('passwordForm').hidden=!session.enabled;el('logoutAccount').hidden=!session.enabled;dialog.showModal();};
 el('account').textContent=session.enabled?session.username:'账号';el('account').onclick=open;el('accountManage').onclick=open;
 el('closeAccount').onclick=()=>dialog.close();
 el('logoutAccount').onclick=async()=>{el('logoutAccount').disabled=true;try{await window.chatLogout();}catch(error){status.textContent=error.message;el('logoutAccount').disabled=false;}};
 el('passwordForm').onsubmit=async event=>{
  event.preventDefault();if(el('newPassword').value!==el('repeatPassword').value){status.textContent='两次新密码不一致';return;}
  const button=el('savePassword');button.disabled=true;status.textContent='正在修改…';
  try{const response=await fetch('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:el('currentPassword').value,new_password:el('newPassword').value}),signal:AbortSignal.timeout(20000)});const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'修改失败，请重试');
   await window.chatLogout();
  }catch(error){status.textContent=error.message;}finally{button.disabled=false;}
 };
 dialog.addEventListener('close',()=>el('passwordForm').reset());
})();
