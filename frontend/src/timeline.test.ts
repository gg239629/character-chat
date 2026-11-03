import { describe,it,expect } from 'vitest';
import { Timeline } from './timeline';
import type { Message } from './types';
const message=(id:string):Message=>({id,role:'assistant',content:id,created_at:1});
describe('timeline',()=>{
 it('rejects a stale poll after clearing',()=>{const state=new Timeline(),epoch=state.epoch;state.clear();expect(state.receive([message('old')],false,epoch)).toBe(false);expect(state.messages).toEqual([]);});
 it('deduplicates deltas and preserves accepted originals',()=>{const state=new Timeline();state.receive([message('a')],false,0);state.receive([message('b'),message('b')],true,0);expect(state.messages.map(m=>m.id)).toEqual(['a','b']);});
 it('replaces optimistic originals by the server message identity',()=>{const state=new Timeline();state.messages=[{...message('a'),pending:true}];state.receive([message('a')],true,0);expect(state.messages).toHaveLength(1);expect(state.messages[0].pending).toBeUndefined();});
});
