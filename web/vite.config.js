import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `vercel dev` routes /api directly to the backend service. A standalone Vite
// process can optionally proxy to a developer-supplied API URL; production
// browser requests always stay on the same-origin /api path.
const standaloneApiUrl = process.env.LOCAL_API_URL

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    ...(standaloneApiUrl ? { proxy: {
      '/api': {
        target: standaloneApiUrl,
        changeOrigin: true,
      },
    } } : {}),
  },
})
