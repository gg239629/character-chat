import {test,expect} from '@playwright/test';
test('group goal mode is optional and saved without showing evaluation results',async({page})=>{
 await page.request.post('/api/auth/login',{data:{username:'browser_admin',password:'browser-test-password-123'}});
 await page.setViewportSize({width:390,height:844});await page.goto('/chat?conversation=default&channel=group');
 await page.getByRole('button',{name:'设置',exact:true}).click();
 const dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'群聊',exact:true}).click();
 await expect(dialog.getByLabel('目标模式',{exact:true})).not.toBeChecked();
 await dialog.getByLabel('目标模式',{exact:true}).check();await dialog.getByRole('button',{name:'保存群聊设置'}).click();
 await expect(dialog.getByRole('status')).toContainText('已保存');
 expect(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
 await page.screenshot({path:'../.test-tmp/goal-mode-mobile.png'});
 await page.reload();await page.getByRole('button',{name:'设置',exact:true}).click();
 await page.getByRole('dialog').getByRole('button',{name:'群聊',exact:true}).click();
 await expect(page.getByLabel('目标模式',{exact:true})).toBeChecked();
});
