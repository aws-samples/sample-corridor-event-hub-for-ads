/**
 * Vite config for the corridor strip UI.
 *
 * The `/api` proxy is what makes this app same-origin with the Python dev server,
 * so no CORS handling is needed in the normal path and the app can be written
 * against a plain relative `fetch('/api/strip')` - exactly the call it will make
 * against the real query API later, with only the base URL changing.
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/** Keep in sync with strip_server.DEFAULT_PORT. */
const API_PORT = process.env.CEH_API_PORT ?? '8787';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Fail loudly rather than silently moving to another port: a second instance
    // on :5174 talking to the same API is a confusing thing to debug.
    strictPort: true,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${API_PORT}`,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
