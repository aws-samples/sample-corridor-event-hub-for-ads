/**
 * Vite config for the record lifecycle tracker.
 *
 * DIFFERENT PORTS FROM ui/ ON PURPOSE - 5174/8788 rather than 5173/8787. The two
 * apps are meant to run at the same time: the strip answers "what is on the
 * corridor", the tracker answers "what happened to this record", and following a
 * record from one to the other is the normal workflow. Sharing a port would make
 * that an either/or, and worse, would let a stale server from one app serve the
 * other.
 *
 * The `/api` proxy keeps the app same-origin with the Python dev server, so the
 * front end is written against a plain relative `fetch('/api/records')` - the same
 * call it would make against the deployed query API with only the base URL
 * changing.
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/** Keep in sync with trace_server.DEFAULT_PORT. */
const API_PORT = process.env.CEH_TRACE_API_PORT ?? '8788';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    // Fail loudly rather than sliding to another port: a second instance talking to
    // the same API is a confusing thing to debug, and the strip UI is already on
    // 5173 next door.
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
