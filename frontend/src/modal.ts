import { nextTick,onMounted,onUnmounted,type Ref } from 'vue';
export function useModal(element:Ref<HTMLElement|undefined>,close:()=>void){
 let previous:HTMLElement|null=null;
 function key(event:KeyboardEvent){
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();return;}
  if(event.key!=='Tab'||!element.value)return;
  const focusable=[...element.value.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]')].filter(e=>e.offsetParent!==null);
  const first=focusable[0],last=focusable.at(-1);if(!first)return;
  if(!element.value.contains(document.activeElement)||!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
 }
 onMounted(async()=>{previous=document.activeElement as HTMLElement;document.addEventListener('keydown',key,true);await nextTick();element.value?.querySelector<HTMLElement>('button,input')?.focus();});
 onUnmounted(()=>{document.removeEventListener('keydown',key,true);previous?.focus();});
}
