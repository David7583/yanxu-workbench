import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({ root: 'frontend', plugins: [react()], build: { outDir: 'dist', emptyOutDir: true, rollupOptions: { output: { manualChunks: { editor: ['@tiptap/react','@tiptap/starter-kit','@tiptap/markdown'], markdown: ['react-markdown','remark-gfm'] } } } }, server: { port: 5173, strictPort: true, proxy: { '/api': 'http://127.0.0.1:8765' } } });
