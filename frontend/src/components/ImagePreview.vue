<script setup lang="ts">
import Icon from './Icon.vue';
import IconButton from './IconButton.vue';
import { ref } from 'vue';import { useModal } from '../modal';
defineProps<{identity:string;alt:string}>();const emit=defineEmits<{close:[]}>(),dialog=ref<HTMLElement>();useModal(dialog,()=>emit('close'));
</script>
<template><div class="overlay" @click.self="emit('close')"><section ref="dialog" class="dialog image-preview" role="dialog" aria-modal="true" aria-label="图片预览"><header><h2>图片预览</h2><IconButton icon="close" label="关闭" @click="emit('close')"/></header><img :src="'/api/images/'+identity+'/content'" :alt="alt"><footer><a class="download-image icon-button" aria-label="保存图片" title="保存图片" :href="'/api/images/'+identity+'/content'" :download="'image-'+identity"><Icon name="download"/></a></footer></section></div></template>
