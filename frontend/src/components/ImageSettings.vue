<script setup lang="ts">
import IconButton from './IconButton.vue';
import { ref,onMounted } from 'vue';
import { api,json } from '../api';
const cfg=ref({enabled:false,inherit_connection:true,model:'gpt-image-2.5',base_url:'',api_key:'',has_key:false});
const busy=ref(false),status=ref('正在读取…');
onMounted(async()=>{try{cfg.value={...cfg.value,...await api<typeof cfg.value>('/api/image-settings'),api_key:''};status.value='';}catch(e){status.value=(e as Error).message;}});
async function save(){busy.value=true;try{cfg.value={...cfg.value,...await api<typeof cfg.value>('/api/image-settings',{method:'PUT',body:json(cfg.value)}),api_key:''};status.value='生图设置已保存';}catch(e){status.value=(e as Error).message;}finally{busy.value=false;}}
</script>
<template><form class="image-settings" @submit.prevent="save"><label class="switch-row"><span>启用聊天生图</span><input class="switch-input" type="checkbox" v-model="cfg.enabled"></label><label>图片模型<input v-model.trim="cfg.model" placeholder="gpt-image-2.5" maxlength="200" required></label><label class="checks"><input type="checkbox" v-model="cfg.inherit_connection">沿用当前聊天的接口和密钥</label><template v-if="!cfg.inherit_connection"><label>生图接口地址<input v-model.trim="cfg.base_url" placeholder="http://192.168.95.1:56828/v1" maxlength="2000" required></label><label>API Key<input v-model.trim="cfg.api_key" type="password" autocomplete="off" :placeholder="cfg.has_key?'已保存，留空保持不变':'填写生图服务密钥'" maxlength="4000"></label></template><p class="muted">在聊天中点击“图片”，或发送“发张自拍”“生成一张风景图”。每次生成一张，费用由图片模型服务收取。</p><p class="feedback" role="status" :title="status">{{status}}</p><div class="actions"><IconButton class="primary" icon="save" label="保存生图设置" type="submit" :busy="busy"/></div></form></template>
