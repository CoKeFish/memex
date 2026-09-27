import path from "path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // Mismo origen que en prod (reverse proxy): el browser pega a /api/* y acá lo
    // redirigimos al backend FastAPI, quitando el prefijo /api. Así no hace falta CORS.
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8787",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
})
