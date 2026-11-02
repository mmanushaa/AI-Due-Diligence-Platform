import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The UI talks to the API on the same origin: in dev (and in docker compose)
// Vite proxies /api to the backend, so the browser never needs CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
