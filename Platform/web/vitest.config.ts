import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(here, 'src'),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
  },
});
