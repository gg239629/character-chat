import type { Message } from './types';
export class Timeline {
 epoch=0;messages:Message[]=[];
 clear(){this.epoch++;this.messages=[];}
 receive(incoming:Message[],delta:boolean,epoch:number){
  if(epoch!==this.epoch)return false;
  const optimistic=this.messages.filter(m=>m.pending);
  const records=delta?[...this.messages.filter(m=>!m.pending),...incoming]:incoming;
  const byId=new Map(records.map(m=>[m.id,m]));
  for(const item of optimistic)if(!byId.has(item.id))byId.set(item.id,item);
  this.messages=[...byId.values()].sort((a,b)=>a.created_at-b.created_at||a.id.localeCompare(b.id));return true;
 }
}
