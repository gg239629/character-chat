import { test,expect } from '@playwright/test';
test('image messages persist, preview by keyboard, and the composer works on mobile',async({page})=>{
 await page.request.post('/api/auth/login',{data:{username:'browser_admin',password:'browser-test-password-123'}});
 const seeded=await (await page.request.post('/__test__/image')).json();
 await page.goto('/chat?conversation=default&channel=liu');
 const image=page.getByRole('button',{name:'查看生成图片'}).last();
 await expect(image).toBeVisible();await expect.poll(()=>image.locator('img').evaluate(e=>(e as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
 await expect(image.locator('..').locator(':scope > p')).toHaveCount(0);
 await image.click();await expect(page.getByRole('dialog',{name:'图片预览'})).toBeVisible();
 await expect(page.getByRole('link',{name:'保存图片'})).toHaveAttribute('href',`/api/images/${seeded.id}/content`);
 await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0);
 await page.reload();await expect(page.getByRole('button',{name:'查看生成图片'}).last()).toBeVisible();
 await expect.poll(()=>page.getByRole('button',{name:'查看生成图片'}).last().locator('img').evaluate(e=>(e as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
 await page.screenshot({path:'../.test-tmp/chat-image-desktop.png'});
 await page.setViewportSize({width:390,height:844});
 await page.getByRole('button',{name:'图片',exact:true}).click();
 const dialog=page.getByRole('dialog',{name:'生成图片'});
 await expect(dialog).toBeVisible();await dialog.getByLabel('想看到什么').fill('画一张清晨的阳台照片');
 await dialog.getByLabel('画面比例').selectOption('1024x1536');
 await page.screenshot({path:'../.test-tmp/chat-image-mobile.png'});
 let submitted:any;
 await page.route('**/api/images',async route=>{
  if(route.request().method()==='POST'){submitted=route.request().postDataJSON();await route.fulfill({status:202,json:seeded});}
  else await route.continue();
 });
 await dialog.getByRole('button',{name:'生成一张'}).click();await expect(dialog).toHaveCount(0);
 expect(submitted.prompt).toBe('画一张清晨的阳台照片');expect(submitted.size).toBe('1024x1536');
 expect(submitted.actor).toBe('liu');expect(submitted.request_id).toBeTruthy();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 let sent:any;
 await page.route('**/api/send',async route=>{sent=route.request().postDataJSON();await route.fulfill({json:{id:'image-tool-test',message_id:'accepted-image-tool-test'}});});
 await page.route('**/api/send/image-tool-test',async route=>{await route.fulfill({json:{status:'done'}});});
 await page.getByRole('textbox',{name:'输入消息'}).fill('发张自拍');await page.getByRole('button',{name:'发送',exact:true}).click();
 await expect.poll(()=>sent?.messages?.[0]?.content).toBe('发张自拍');
});

test('mobile image settings keep save controls accessible',async({page})=>{
 await page.request.post('/api/auth/login',{data:{username:'browser_admin',password:'browser-test-password-123'}});
 await page.setViewportSize({width:390,height:844});await page.goto('/');
 const guide=page.getByRole('button',{name:'知道了'});if(await guide.isVisible())await guide.click();
 await page.getByRole('button',{name:'设置',exact:true}).first().click();
 const dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'模型',exact:true}).click();await dialog.getByRole('button',{name:'生图',exact:true}).click();
 await expect(dialog.getByLabel('图片模型')).toHaveValue('gpt-image-2.5');
 await expect(dialog.getByRole('button',{name:'保存生图设置'})).toBeVisible();
 expect(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
 await page.screenshot({path:'../.test-tmp/image-settings-mobile.png'});
});


test('models share one settings tab and voice IDs start empty',async({page})=>{
 await page.request.post('/api/auth/login',{data:{username:'browser_admin',password:'browser-test-password-123'}});
 await page.setViewportSize({width:390,height:844});await page.goto('/');
 const guide=page.getByRole('button',{name:'知道了'});if(await guide.isVisible())await guide.click();
 await page.getByRole('button',{name:'设置',exact:true}).first().click();
 const dialog=page.getByRole('dialog');
 await dialog.getByRole('button',{name:'语音',exact:true}).click();
 await expect(dialog.getByLabel('语音模型',{exact:true})).toHaveValue('');
 await expect(dialog.getByLabel('启用聊天语音')).not.toBeChecked();
 await dialog.getByLabel('启用聊天语音').check();
 await dialog.getByLabel('语音模型',{exact:true}).fill('user-speech-model');
 await dialog.getByRole('button',{name:'保存语音设置'}).click();
 await expect(dialog.getByRole('status').last()).toContainText('语音模型设置已保存');
 await page.route('**/api/speech-settings/test',async route=>{await route.fulfill({json:{ok:true,level:'model',message:'连接正常，模型可访问；尚未验证语音合成。'}});});
 await dialog.getByRole('button',{name:'测试语音连接'}).click();
 await expect(dialog.getByRole('status').last()).toContainText('连接正常');
 expect(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
 await page.screenshot({path:'../.test-tmp/speech-settings-mobile.png'});
 await dialog.getByRole('button',{name:'关闭设置'}).click();
 await page.goto('/chat?conversation=default&channel=liu');
 await page.getByRole('button',{name:'设置',exact:true}).click();
 const character=page.getByRole('dialog');await character.getByRole('button',{name:'人物',exact:true}).click();
 await character.getByRole('button',{name:'下一页'}).click();
 await expect(character.getByLabel('基础音色',{exact:true})).toHaveValue('');
 await character.getByLabel('基础音色',{exact:true}).fill('user-voice-liu');
 await character.getByRole('button',{name:'保存人物设定'}).click();
 await expect(character.getByRole('status')).toContainText('已保存');
 await page.screenshot({path:'../.test-tmp/character-voice-mobile.png'});
});
