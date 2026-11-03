import { onMounted,onUnmounted } from 'vue';
// One connection per active page. Reconnect takes a fresh permission-filtered snapshot.
export function useEvents(refresh:()=>void){
 let source:EventSource|undefined,timer:ReturnType<typeof setTimeout>|undefined,closed=false;
 function changed(){if(timer)clearTimeout(timer);timer=setTimeout(()=>{timer=undefined;if(!closed&&!document.hidden)refresh();},120);}
 function visibility(){source?.close();source=undefined;if(closed||document.hidden||!('EventSource' in window))return;source=new EventSource('/api/events');source.addEventListener('ready',changed);source.addEventListener('changed',changed);}
 onMounted(()=>{visibility();document.addEventListener('visibilitychange',visibility);});
 onUnmounted(()=>{closed=true;source?.close();if(timer)clearTimeout(timer);document.removeEventListener('visibilitychange',visibility);});
}
