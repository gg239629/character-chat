<script setup lang="ts">
import IconButton from './IconButton.vue';
import { ref } from 'vue';
import { useModal } from '../modal';
const props=defineProps<{initial:string;channel:string;busy:boolean;error:string}>();
const emit=defineEmits<{close:[];generate:[prompt:string,size:string,actor:string]}>();
const dialog=ref<HTMLElement>(),prompt=ref(props.initial),size=ref('1024x1024'),actor=ref(props.channel==='group'?'':props.channel);
useModal(dialog,()=>{if(!props.busy)emit('close');});
</script>
<template><div class="overlay" @click.self="!busy&&emit('close')"><form ref="dialog" class="dialog image-composer" role="dialog" aria-modal="true" aria-labelledby="imageTitle" @submit.prevent="emit('generate',prompt.trim(),size,actor)"><header><h2 id="imageTitle">生成图片</h2><IconButton icon="close" label="关闭" @click="emit('close')" :disabled="busy"/></header><label>想看到什么<textarea v-model="prompt" rows="4" maxlength="4000" placeholder="例如：一张在阳台看花的自拍，清晨自然光" required></textarea></label><div class="two-col"><label>画面比例<select v-model="size"><option value="1024x1024">方形 · 1:1</option><option value="1024x1536">竖图 · 2:3</option><option value="1536x1024">横图 · 3:2</option><option value="auto">自动</option></select></label><label v-if="channel==='group'">人物<select v-model="actor"><option value="">只画场景</option><option value="wan">小婉</option><option value="yan">小研</option><option value="liu">小柳</option></select></label></div><p class="muted">会参考当前人物的外观和本聊天的场景。图片完成后保存在聊天中，生成期间可以继续聊天。</p><p class="image-error" role="status">{{error}}</p><footer><IconButton class="primary" icon="sparkles" label="生成一张" type="submit" :busy="busy" :disabled="!prompt.trim()"/></footer></form></div></template>
