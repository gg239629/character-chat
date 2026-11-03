import { test,expect } from '@playwright/test';
test.beforeEach(async({page})=>{await page.request.post('/api/auth/login',{data:{username:'browser_admin',password:'browser-test-password-123'}});await page.addInitScript(()=>{const now=new Date();localStorage.setItem('chat-usage-guide:'+'4c1029697ee358715d3a14a2'+':'+now.toLocaleDateString(),'shown');});});
async function closeGuide(page:import('@playwright/test').Page){await expect(page.locator('.story')).toHaveCount(4);const guide=page.getByRole('button',{name:'知道了'});if(await guide.isVisible())await guide.click();}
test('homepage and chat settings inherit models without losing controls',async({page})=>{
 await page.goto('/');await closeGuide(page);await expect(page.locator('.story')).toHaveCount(4);
 await page.locator('.story').filter({hasText:'小研'}).first().click();await expect(page.getByRole('button',{name:'发送',exact:true})).toBeVisible();
 await page.getByRole('button',{name:'设置',exact:true}).click();const modal=page.getByRole('dialog');
 const inherit=modal.getByRole('switch',{name:'使用主页默认模型'});await expect(inherit).toBeChecked();
 await expect(modal.getByLabel('接口地址')).toBeDisabled();await inherit.uncheck();await expect(modal.getByLabel('接口地址')).toBeEnabled();
 await modal.getByRole('button',{name:'资料',exact:true}).click();await expect(modal.getByLabel('会话名称')).toBeVisible();
 await page.screenshot({path:'../.test-tmp/settings-desktop.png'});
 await modal.getByRole('button',{name:'关闭',exact:true}).click();await expect(page.getByRole('link',{name:'返回主页'})).toBeVisible();
});
test('messages can be sent consecutively and clear survives reentry',async({page})=>{
 await page.goto('/chat?conversation=default&channel=yan');await expect(page.getByRole('button',{name:'发送',exact:true})).toBeVisible();
 const input=page.getByRole('textbox',{name:'输入消息'});
 await input.fill('第一条浏览器测试');await page.getByRole('button',{name:'发送',exact:true}).click();
 await input.fill('第二条浏览器测试');await page.getByRole('button',{name:'发送',exact:true}).click();
 await expect(page.locator('.message.mine')).toHaveCount(2);await expect(page.locator('.message.pending')).toHaveCount(0,{timeout:10000});
 await page.getByRole('button',{name:'设置',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'显示',exact:true}).click();await page.getByRole('button',{name:'清空当前显示',exact:true}).click();await expect(page.locator('.message')).toHaveCount(0);await page.reload();await expect(page.locator('.message')).toHaveCount(0);
});
test('MCP authorization and mobile layout',async({page})=>{
 await page.setViewportSize({width:390,height:844});await page.goto('/');await closeGuide(page);
 await page.getByRole('button',{name:'设置',exact:true}).first().click();const modal=page.getByRole('dialog');
 await modal.getByRole('button',{name:'MCP',exact:true}).click();await modal.getByLabel('客户端名称').fill('浏览器回归客户端');await modal.getByLabel('每日 Token 预算（0 不限制）').fill('5000');await modal.getByRole('button',{name:'生成密钥',exact:true}).click();await expect(modal.getByRole('textbox',{name:'MCP 密钥'})).toHaveValue(/^ccm_/);
 await modal.getByRole('button',{name:'已授权客户端',exact:true}).click();await expect(modal.getByText('浏览器回归客户端')).toBeVisible();await modal.getByRole('button',{name:'撤销',exact:true}).first().click();await expect(modal.getByRole('button',{name:'已撤销',exact:true}).first()).toBeVisible();
 expect(await modal.evaluate(e=>e.scrollHeight<=e.clientHeight+1)).toBe(true);
 await page.screenshot({path:'../.test-tmp/settings-mobile.png'});
});
test('Vue registration and login enter an independent account',async({page,context})=>{
 await context.clearCookies();await page.goto('/login');
 await page.getByRole('button',{name:'注册',exact:true}).click();
 await page.getByLabel('用户名',{exact:true}).fill('new_browser_user');
 await page.getByLabel('密码',{exact:true}).fill('new-browser-password-123');
 await page.getByLabel('确认密码',{exact:true}).fill('new-browser-password-123');
 await page.getByRole('button',{name:'创建账号',exact:true}).click();
 await expect(page.getByRole('status')).toContainText('注册成功');
 await page.getByLabel('密码',{exact:true}).fill('new-browser-password-123');
 await page.getByRole('button',{name:'登录',exact:true}).last().click();
 await expect(page).toHaveURL(/\/$/);await closeGuide(page);
 await expect(page.locator('.story')).toHaveCount(4);
 await expect(page.getByRole('button',{name:'new_browser_user',exact:true})).toBeVisible();
});

test('SSE delivers a background message without waiting for fallback polling',async({page})=>{
 const connected=page.waitForResponse(r=>r.url().endsWith('/api/events'));
 await page.goto('/');await closeGuide(page);await connected;
 const result=await page.request.post('/__test__/message');expect(result.ok()).toBe(true);
 await expect(page.locator('.story').filter({hasText:'今天一起去植物园吧。'})).toHaveCount(1,{timeout:5000});
});
