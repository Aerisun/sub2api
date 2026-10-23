import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";
export default defineConfig({
  plugins: [tailwindcss()],
  base: "./",
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: { outDir: "../frontend/public/chat-ui", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    port: 5198,
    proxy: { "/api": process.env.CHAT_API_TARGET || "http://127.0.0.1:8080" },
  },
});
