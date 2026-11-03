<script setup lang="ts">
import Icon from './Icon.vue';import type { IconName } from '../icons';
withDefaults(defineProps<{icon:IconName;label:string;busy?:boolean;disabled?:boolean;type?:'button'|'submit'|'reset'}>(),{busy:false,disabled:false,type:'button'});
</script>
<template><button class="icon-button" :type="type" :aria-label="label" :title="label" :aria-busy="busy||undefined" :disabled="disabled||busy"><Icon :name="busy?'loader':icon" :class="{'icon-spinning':busy}"/></button></template>
