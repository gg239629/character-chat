(()=>{
 const dayKey=(scope,now=new Date())=>'chat-usage-guide:'+scope+':'+[now.getFullYear(),now.getMonth()+1,now.getDate()].join('-');
 window.chatTutorialDayKey=dayKey;
 async function show(){
  if(window.chatSessionReady&&!await window.chatSessionReady)return;
  if(document.hidden)return;
  try{
   const r=await fetch('/api/home/model',{cache:'no-store'});if(!r.ok)return;
   const data=await r.json(),key=dayKey(data.tutorial_scope||'personal');
   try{if(localStorage.getItem(key)==='shown')return;}catch{}
   if(document.querySelector('dialog[open]'))return;
   const dialog=document.createElement('dialog');dialog.setAttribute('aria-labelledby','usageGuideTitle');
   dialog.style.cssText='background:#202020;color:#eee;border:1px solid #444;border-radius:8px;padding:24px;width:min(440px,calc(100% - 32px));max-height:85dvh;overflow:auto';
   dialog.innerHTML=`<h2 id="usageGuideTitle" style="margin-top:0">使用教程</h2><ol style="padding-left:22px;line-height:1.9"><li>在主页左下角打开“设置 → 大模型配置”，填写接口地址、模型名称和 API Key，测试后保存。</li><li>群聊、三位角色及后台功能默认使用主页模型。需要不同模型时，到对应会话设置中关闭“使用主页默认模型”再配置。</li><li>聊天时可连续发送消息；主页设置可关闭角色主动消息，避免闲置时持续调用。</li><li>重置聊天会删除当前故事的聊天与记忆，请确认后操作。</li></ol><p style="color:#bbb">本浏览器每天首次打开时显示一次。</p><div style="display:flex;justify-content:flex-end;gap:12px;margin-top:24px"><button id="usageGuideClose" style="padding:10px 16px">知道了</button><button id="usageGuideSetup" style="padding:10px 16px;background:#07c160;color:white;border:0;border-radius:4px">去配置</button></div>`;
   document.body.append(dialog);dialog.showModal();
   try{localStorage.setItem(key,'shown');}catch{}
   dialog.querySelector('#usageGuideClose').onclick=()=>dialog.close();
   dialog.querySelector('#usageGuideSetup').onclick=()=>{dialog.close();if(window.openModelDefaults)window.openModelDefaults();else location.assign('/?setup=model');};
  }catch{}
 }
 show();document.addEventListener('visibilitychange',()=>{if(!document.hidden)show();});
})();
