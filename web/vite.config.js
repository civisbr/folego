import { defineConfig } from "vite";
export default defineConfig({
  base: "./",
  publicDir: "public",
  build: { chunkSizeWarningLimit: 1000 },
});
