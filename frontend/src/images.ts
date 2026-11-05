import type { Message } from './types';
export interface ImageJob {id:string;status:'pending'|'running'|'done'|'failed';actor:string;prompt:string;size:string;error:string;message_id:string;created_at:number}
export const imageMarker=/\[\[image:([0-9a-f]{64})\]\]/;
export function messageImage(message:Message){return message.role==='assistant'?message.content.match(imageMarker)?.[1]||'':'';}
export function visibleMessageText(text:string){return text.split(/\[\[\s*(?:action\s*:\s*image|image\s*:)[^\]\n]*\]\]/i,1)[0].trim();}
