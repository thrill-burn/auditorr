import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Keep function names through minification, so a render error names the
  // component it happened in (ErrorBoundary.jsx shows and logs that chain).
  // Minified, the chain reads `Qe › ft › b`.
  esbuild: { keepNames: true },
  server: {
    proxy: { '/api': 'http://localhost:8677' },
  },
})
