<script setup lang="ts">
import { icons,type IconName } from '../icons';
withDefaults(defineProps<{name:IconName;size?:number}>(),{size:20});
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path v-for="(d,i) in icons[name]" :key="i" :d="d"/></svg></template>
