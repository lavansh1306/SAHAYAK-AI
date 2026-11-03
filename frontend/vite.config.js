import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const API = process.env.SAHAYAK_API || 'http://127.0.0.1:8000'

export default defineConfig({
  build: { chunkSizeWarningLimit: 2000 },
  plugins: [react()],
  server: { port: 5173, strictPort: false, proxy: { '/api': { target: API, changeOrigin: true } } },
  preview: { port: 5173, proxy: { '/api': { target: API, changeOrigin: true } } },
})
