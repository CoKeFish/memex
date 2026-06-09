import { fileURLToPath } from "node:url"
import { defineConfig } from "vitest/config"

// Config mínima para los tests unitarios (lib pura). Entorno node (las funciones bajo test usan solo
// Intl/Date); include acotado a *.test.ts para no levantar los plugins de react/tailwind del build.
// El alias `@` se resuelve a mano (acá no corre el plugin de Vite que lo aporta en el build) para
// poder testear libs que importan otras vía `@/…` (p. ej. inbox-format → attachment-kind).
export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
})
