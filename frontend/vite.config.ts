import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
export default defineConfig({plugins:[vue()],base:'/web/',build:{outDir:'../static/web',emptyOutDir:true},server:{proxy:{'/api':'http://127.0.0.1:8788','/static':'http://127.0.0.1:8788','/sw.js':'http://127.0.0.1:8788','/mcp':'http://127.0.0.1:8788'}}});
