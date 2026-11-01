/* Shared push registration; permission requests only follow a user gesture. */
window.chatNotifications={
  enabled(){
    return 'Notification' in window && Notification.permission==='granted' &&
      localStorage.getItem('companion-notifications-disabled')!=='true';
  },
  async enable(){
    if(!('Notification' in window))throw Error('当前浏览器不支持通知');
    const permission=await Notification.requestPermission();
    if(permission!=='granted')throw Error('请在浏览器站点设置中允许通知');
    localStorage.removeItem('companion-notifications-disabled');
    if(!('serviceWorker' in navigator) || !('PushManager' in window))return '已开启页面通知，此浏览器暂不支持后台推送';
    const response=await fetch('/api/push/config');
    if(!response.ok)throw Error('读取推送配置失败');
    const config=await response.json();
    if(!config.public_key)return '页面通知已开启；服务器尚未配置后台推送密钥';
    const registration=await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    let subscription=await registration.pushManager.getSubscription();
    if(!subscription){
      const value=config.public_key.replace(/-/g,'+').replace(/_/g,'/');
      const key=Uint8Array.from(atob(value+'='.repeat((4-value.length%4)%4)),c=>c.charCodeAt(0));
      subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key});
    }
    const saved=await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(subscription)});
    if(!saved.ok)throw Error('保存推送订阅失败，请检查服务器加密及推送配置');
    localStorage.setItem('companion-web-push','enabled');
    return '后台推送已开启；服务器运行时可发送通知';
  },
  async disable(){
    // Stop page notifications immediately, independently of the push server.
    localStorage.removeItem('companion-web-push');
    localStorage.setItem('companion-notifications-disabled','true');
    localStorage.setItem('companion-notifications-disable-pending','true');
    if('serviceWorker' in navigator){
      const registration=await navigator.serviceWorker.getRegistration('/');
      const subscription=await registration?.pushManager.getSubscription();
      if(subscription){
        // Either removal prevents future pushes; try both even if one fails.
        const results=await Promise.allSettled([
          fetch('/api/push/unsubscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:subscription.endpoint}),signal:AbortSignal.timeout(10000)}).then(r=>{if(!r.ok)throw Error('取消订阅失败');return true;}),
          subscription.unsubscribe().then(ok=>{if(!ok)throw Error('取消订阅失败');return true;})
        ]);
        if(results.every(r=>r.status==='rejected'))throw Error('页面通知已关闭，后台推送未能关闭，请再次点击关闭通知重试');
      }
    }
    localStorage.removeItem('companion-notifications-disable-pending');
    return '本设备通知已关闭';
  },
  bind(button,status){
    let retryDisable=localStorage.getItem('companion-notifications-disable-pending')==='true';
    const render=()=>{const enabled=this.enabled()||retryDisable;button.textContent=enabled?'关闭通知':'开启通知';button.setAttribute('aria-pressed',String(enabled));};
    render();
    button.onclick=async()=>{
      const closing=this.enabled()||retryDisable;button.disabled=true;
      try{status.textContent=await (closing?this.disable():this.enable());retryDisable=false;}
      catch(e){status.textContent=e.message;if(closing)retryDisable=true;}
      finally{button.disabled=false;render();}
    };
    window.addEventListener('storage',render);
    window.addEventListener('pageshow',render);
  }
};
