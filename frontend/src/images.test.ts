import { describe,it,expect } from 'vitest';
import { messageImage,visibleMessageText } from './images';
describe('chat images',()=>{
 it('hides legacy internal image markers and trailing prompts',()=>{
  expect(visibleMessageText('等我一下。\n[[action:image]]\n内部画面描述')).toBe('等我一下。');
  expect(visibleMessageText('普通聊天（拿起手机）')).toBe('普通聊天（拿起手机）');
 });
 it('renders only controlled assistant image identifiers',()=>{
  const id='a'.repeat(64);
  expect(messageImage({id:'one',role:'assistant',content:`照片[[image:${id}]]`,created_at:1})).toBe(id);
  expect(messageImage({id:'one',role:'user',content:`[[image:${id}]]`,created_at:1})).toBe('');
  expect(messageImage({id:'one',role:'assistant',content:'<img src="javascript:bad">[[image:../secrets]]',created_at:1})).toBe('');
 });
});
