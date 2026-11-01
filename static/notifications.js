/* Shared push registration; permission requests only follow a user gesture. */
window.chatNotifications={
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
    if('serviceWorker' in navigator){
      const registration=await navigator.serviceWorker.getRegistration('/');
      const subscription=await registration?.pushManager.getSubscription();
      if(subscription){
        const response=await fetch('/api/push/unsubscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:subscription.endpoint})});
        if(!response.ok)throw Error('关闭通知失败，请重试');
        await subscription.unsubscribe();
      }
    }
    localStorage.removeItem('companion-web-push');
    localStorage.setItem('companion-notifications-disabled','true');
  }
};
