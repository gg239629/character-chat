import {test,expect} from '@playwright/test';
test('voice reply plays on demand and transcript is collapsible',async({page})=>{
 await page.request.post('/api/auth/login',{data:{username:'browser_admin',password:'browser-test-password-123'}});
 await page.request.post('/__test__/voice');
 await page.setViewportSize({width:390,height:844});await page.goto('/chat?conversation=default&channel=liu');
 const voice=page.getByRole('region',{name:'角色语音'});
 const audio=page.locator('.voice-message audio');
 await expect(audio).toBeVisible();await expect(audio).toHaveAttribute('controls','');
 expect(await audio.getAttribute('autoplay')).toBeNull();
 await expect(audio).toHaveAttribute('preload','metadata');
 await expect.poll(()=>audio.evaluate((element:HTMLAudioElement)=>Number.isFinite(element.duration)?element.duration:0)).toBeGreaterThan(0);
 expect(await audio.evaluate((element:HTMLAudioElement)=>element.paused)).toBe(true);
 const transcript=page.locator('.voice-transcript');await expect(transcript).not.toBeVisible();
 await page.getByText('查看文字',{exact:true}).click();await expect(transcript).toHaveText('（洗手）等我一下。');
 await audio.evaluate((element:HTMLAudioElement)=>element.play());
 await expect.poll(()=>audio.evaluate((element:HTMLAudioElement)=>element.readyState)).toBeGreaterThan(1);
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 await page.screenshot({path:'../.test-tmp/voice-message-mobile.png'});
 await page.reload();await expect(page.locator('.voice-message audio')).toBeVisible();
});

test('private reply mode is saved explicitly',async({page})=>{
 await page.request.post('/api/auth/login',{data:{username:'browser_admin',password:'browser-test-password-123'}});
 await page.setViewportSize({width:390,height:844});await page.goto('/chat?conversation=default&channel=wan');
 await page.getByRole('button',{name:'设置',exact:true}).click();
 const dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'回复',exact:true}).click();
 await expect(dialog.getByLabel('回复方式')).toHaveValue('text');
 await dialog.getByLabel('回复方式').selectOption('voice');
 await dialog.getByRole('button',{name:'保存回复设置'}).click();
 await expect(dialog.getByRole('status')).toContainText('已保存');
 expect(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
 await page.screenshot({path:'../.test-tmp/private-reply-mode-mobile.png'});
 await page.reload();await page.getByRole('button',{name:'设置',exact:true}).click();
 await page.getByRole('dialog').getByRole('button',{name:'回复',exact:true}).click();
 await expect(page.getByLabel('回复方式')).toHaveValue('voice');
});
