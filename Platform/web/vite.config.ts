import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const here = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  plugins: [react()],
  root: here,
  build: {
    outDir: resolve(here, '../dist/web'),
    emptyDir: true,
    sourcemap: true,
  },
  base: '/platform/',
  /** 与 `npm run start`（默认 8787）联调：先起 Platform，再 `npm run dev` 本目录 */
  server: {
    port: 5173,
    proxy: {
      '^/(internal|health|ping|api)': { target: 'http://127.0.0.1:8787', changeOrigin: true },
    },
  },
});
