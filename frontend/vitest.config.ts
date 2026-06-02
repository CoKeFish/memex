import { defineConfig } from "vitest/config"

// Config mínima para los tests unitarios (lib pura). Entorno node (las funciones bajo test usan solo
// Intl/Date); include acotado a *.test.ts para no levantar los plugins de react/tailwind del build.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
})
