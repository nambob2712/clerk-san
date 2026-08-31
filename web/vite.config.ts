import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const WEB_ROOT = fileURLToPath(new URL(".", import.meta.url));
const BRAND_ASSETS = fileURLToPath(new URL("../assets/brand", import.meta.url));

const LOOPBACK_ORIGINS = new Set([
  "http://127.0.0.1:8000",
]);

function localApiOrigin(value: string | undefined): string {
  const origin = value ?? "http://127.0.0.1:8000";
  if (!LOOPBACK_ORIGINS.has(origin)) {
    throw new Error("CLERKSAN_API_ORIGIN must be an explicitly supported loopback origin.");
  }
  return origin;
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "CLERKSAN_");
  const target = localApiOrigin(env.CLERKSAN_API_ORIGIN);
  // The API validates both browser Origin and the proxied Host. Vite must rewrite
  // the development Host to the loopback API while browser code keeps relative URLs.
  const proxy = { target, changeOrigin: true, secure: false };

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    build: {
      manifest: true,
      sourcemap: false,
      target: "es2022",
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      fs: {
        allow: [WEB_ROOT, BRAND_ASSETS],
      },
      proxy: {
        "/health": proxy,
        "/ready": proxy,
        "/capabilities": proxy,
        "/documents": proxy,
        "/intakes": proxy,
        "/review": proxy,
        "/bills": proxy,
        "/query": proxy,
        "/openapi.json": proxy,
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      clearMocks: true,
      exclude: ["e2e/**", "**/node_modules/**", "**/dist/**"],
    },
  };
});
