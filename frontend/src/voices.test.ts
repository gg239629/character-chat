import {describe,it,expect} from 'vitest';
import {messageVoice,voiceTranscript} from './voices';
describe('voice replies',()=>{
 it('accepts only server-shaped assistant IDs and hides the marker in transcript',()=>{
  const id='a'.repeat(64),message={id:'one',role:'assistant' as const,content:`小柳：（擦手）等我一下。\n[[voice:${id}]]`,created_at:1};
  expect(messageVoice(message)).toBe(id);expect(voiceTranscript(message)).toBe('（擦手）等我一下。');
  expect(messageVoice({...message,role:'user'})).toBe('');
  expect(messageVoice({...message,content:'[[voice:../secret]]'})).toBe('');
 });
});
