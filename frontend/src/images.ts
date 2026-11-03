import type { Message } from './types';
export interface ImageJob {id:string;status:'pending'|'running'|'done'|'failed';actor:string;prompt:string;size:string;error:string;message_id:string;created_at:number}
export const imageMarker=/\[\[image:([0-9a-f]{64})\]\]/;
export function messageImage(message:Message){return message.role==='assistant'?message.content.match(imageMarker)?.[1]||'':'';}
export function imageRequest(text:string){
 if(/不要|不用|别给|别发|不需要|不想/.test(text))return false;
 if(/^(?:你|你们|这里|这个|网站).{0,10}(?:能|可以|会|支持).{0,10}(?:生图|生成图片|画画).*[吗么？?]$/.test(text))return false;
 return /(?:画|生成|绘制)(?:一张|一幅|个|张)?[^。！？\n]{0,24}(?:图|画|照片)|(?:发|来|给我|拍)[^。！？\n]{0,12}(?:自拍|照片)|^(?:(?:小婉|小研|小柳)[，,:： ]*)?(?:请|帮我|给我)?(?:画(?:一|个|张|幅|只)|绘制)|^\/image\s+/.test(text);
}
