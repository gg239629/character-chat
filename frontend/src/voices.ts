import type { Message } from './types';
export const voiceMarker=/\[\[voice:([0-9a-f]{64})\]\]/;
export function messageVoice(message:Message){return message.role==='assistant'?message.content.match(voiceMarker)?.[1]||'':'';}
export function voiceTranscript(message:Message){return message.content.replace(voiceMarker,'').replace(/^小[婉研柳][：:]\s*/,'').trim();}
