import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // The browser talks to this dev server only. It is published on the host's
  // :5173 -- the one normal application URL -- and it proxies /api to the
  // FastAPI backend on :8008.
  //
  // The proxy keeps the browser's original host header (changeOrigin is
  // off), so the backend sees the application origin the browser used.
  // That matters for the OIDC redirect URI: the callback is
  // http://localhost:5173/api/auth/callback, and it is served by the
  // backend through this very proxy.
  //
  // The port and the backend address can be overridden (AGENT_UI_PORT /
  // AGENT_DEV_API_TARGET in the environment or in ui/.env).
  const env = loadEnv(mode, process.cwd(), "");
  const uiPort = Number(process.env.AGENT_UI_PORT || env.AGENT_UI_PORT || 5173);
  const apiTarget =
    process.env.AGENT_DEV_API_TARGET || env.AGENT_DEV_API_TARGET || "http://127.0.0.1:8008";

  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: uiPort,
      strictPort: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: false,
        },
        "/readyz": {
          target: apiTarget,
          changeOrigin: false,
        },
        "/healthz": {
          target: apiTarget,
          changeOrigin: false,
        },
      },
    },
  };
});
