import { describe,it,expect } from 'vitest';
import { imageRequest,messageImage } from './images';
describe('chat images',()=>{
 it('recognizes explicit image requests without treating capability questions as paid requests',()=>{
  for(const text of ['给我发张自拍','生成一张风景图','画一只猫','小柳，画一张照片','/image a flower'])expect(imageRequest(text),text).toBe(true);
  for(const text of ['不要发自拍','你能生成图片吗','这张照片很好看','几点了'])expect(imageRequest(text),text).toBe(false);
 });
 it('renders only controlled assistant image identifiers',()=>{
  const id='a'.repeat(64);
  expect(messageImage({id:'one',role:'assistant',content:`照片[[image:${id}]]`,created_at:1})).toBe(id);
  expect(messageImage({id:'one',role:'user',content:`[[image:${id}]]`,created_at:1})).toBe('');
  expect(messageImage({id:'one',role:'assistant',content:'<img src="javascript:bad">[[image:../secrets]]',created_at:1})).toBe('');
 });
});
