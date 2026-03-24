import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function readBackendPort() {
  const env = process.env.VITE_BACKEND_PORT;
  if (env && /^\d+$/.test(env)) return parseInt(env, 10);
  try {
    const p = path.resolve(__dirname, "../config.json");
    const j = JSON.parse(fs.readFileSync(p, "utf8"));
    const port = j?.server?.port;
    return typeof port === "number" && port > 0 && port <= 65535 ? port : 8000;
  } catch {
    return 8000;
  }
}

const backendPort = readBackendPort();
const backendOrigin = `http://127.0.0.1:${backendPort}`;

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backendOrigin, changeOrigin: true },
      "/ws": { target: `ws://127.0.0.1:${backendPort}`, ws: true },
    },
  },
});
