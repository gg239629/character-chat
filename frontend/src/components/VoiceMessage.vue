<script setup lang="ts">
import { ref,onMounted,onUnmounted } from 'vue';
import { api,type Scope } from '../api';
const props=defineProps<{identity:string;transcript:string;scope:Scope}>();
const state=ref('pending'),error=ref(''),playing=ref<HTMLAudioElement>();let stopped=false,busy=false,timer:ReturnType<typeof setInterval>;
async function refresh(){if(stopped||busy||document.hidden)return;busy=true;try{const result=await api<{status:string;error:string}>('/api/voices/'+props.identity,{},props.scope);state.value=result.status;error.value=result.error;if(['done','failed'].includes(state.value))clearInterval(timer);}catch{error.value='暂时无法读取语音，可以先查看文字';}finally{busy=false;}}
onMounted(()=>{refresh();timer=setInterval(refresh,3000);});
onUnmounted(()=>{stopped=true;clearInterval(timer);playing.value?.pause();});
function play(){document.querySelectorAll('audio').forEach(audio=>{if(audio!==playing.value)audio.pause();});}
</script>
<template><section class="voice-message" aria-label="角色语音"><audio v-if="state==='done'" ref="playing" controls preload="metadata" :src="'/api/voices/'+identity+'/content'" aria-label="播放角色语音" @play="play"/><span v-else role="status">{{state==='failed'?'语音没发出来，可以先看文字':'正在录语音…'}}</span><small v-if="error" role="status">{{error}}</small><details :open="state==='failed'"><summary>查看文字</summary><div class="voice-transcript">{{transcript}}</div></details></section></template>
