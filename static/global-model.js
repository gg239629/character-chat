(()=>{
 const dialog=document.createElement('dialog');dialog.setAttribute('aria-labelledby','globalModelTitle');
 dialog.id='globalModelDialog';
 dialog.innerHTML=`<h2 id="globalModelTitle">大模型配置</h2><p class="model-note">角色及辅助功能默认使用此配置，各会话可单独更改。</p>
 <div class="model-field"><label for="globalModelUrl">接口地址</label><input id="globalModelUrl" placeholder="https://api.example.com/v1" autocomplete="off"></div>
 <div class="model-field"><label for="globalModelName">模型名称</label><input id="globalModelName" placeholder="供应商提供的模型名" autocomplete="off"></div>
 <div class="model-field"><label for="globalModelKey">API Key <span id="globalKeyHint"></span></label><input id="globalModelKey" type="password" autocomplete="new-password" placeholder="已有密钥时留空保留"></div>
 <div class="model-field"><label for="globalModelTemp">温度</label><input id="globalModelTemp" type="number" min="0" max="2" step="0.1" value="1"></div>
 <div class="model-field"><label for="globalModelTokens">最大输出长度（0 为不限）</label><input id="globalModelTokens" type="number" min="0" max="100000" value="1500"></div>
 <p id="globalModelStatus" role="status" style="min-height:24px;margin-top:16px"></p><div class="actions"><button class="quiet" id="globalModelClose">关闭</button><button class="quiet" id="globalModelTest">测试连接</button><button class="primary" id="globalModelSave">保存</button></div>`;
 document.body.append(dialog);window.mountSettingsLayout(dialog);
 const el=id=>document.getElementById(id),status=el('globalModelStatus');
 const draft=()=>({base_url:el('globalModelUrl').value.trim(),model:el('globalModelName').value.trim(),api_key:el('globalModelKey').value.trim(),temperature:Number(el('globalModelTemp').value),max_tokens:Number(el('globalModelTokens').value)});
 function fill(data){el('globalModelUrl').value=data.base_url||'';el('globalModelName').value=data.model||'';el('globalModelKey').value='';el('globalKeyHint').textContent=data.api_key_masked||'';el('globalModelTemp').value=data.temperature??1;el('globalModelTokens').value=data.max_tokens??0;}
 function lock(value){dialog.querySelectorAll('input,button').forEach(e=>e.disabled=value);el('globalModelClose').disabled=false;}
 async function call(url,method,body){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'请检查配置后重试');return d;}
 window.openModelDefaults=async()=>{if(!dialog.open)dialog.showModal();lock(true);status.textContent='正在读取…';try{fill(await call('/api/home/model','GET'));status.textContent='保存后各会话默认继承这里；关闭会话内“使用主页默认模型”可独立设置。';}catch(e){status.textContent=e.message;}finally{lock(false);}};
 el('homeModelSettings').onclick=window.openModelDefaults;
 el('globalModelClose').onclick=()=>dialog.close();
 el('globalModelSave').onclick=async()=>{lock(true);status.textContent='正在保存…';try{fill(await call('/api/home/model','PUT',draft()));status.textContent='已保存，继承默认模型的会话下次调用生效。';}catch(e){status.textContent=e.message;}finally{lock(false);}};
 el('globalModelTest').onclick=async()=>{lock(true);status.textContent='正在测试…';try{await call('/api/home/model/test','POST',draft());status.textContent='连接成功';}catch(e){status.textContent=e.message;}finally{lock(false);}};
 if(new URLSearchParams(location.search).get('setup')==='model')Promise.resolve(window.chatSessionReady).then(ready=>{if(ready!==false)window.openModelDefaults();});
})();
