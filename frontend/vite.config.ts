import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const PROXY_PATHS = [
  "/api",
  "/auth",
  "/sessions",
  "/swarm/presets",
  "/swarm/runs",
  "/qveris",
  "/settings/llm",
  "/settings/data-sources",
  "/channels",
  "/mandate",
  "/live",
  "/upload",
  "/shadow-reports",
  "/scheduled-runs",
  "/options",
  // Portfolio dashboard + local broker connections (/api/portfolio, /api/connections)
  "/api",
];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_URL || "http://127.0.0.1:8899";
  const devPort = 5899;
  const apiProxy = {
    target: apiTarget,
    changeOrigin: true,
    // LAN UI Origin is http://<lan-ip>:5899; the API CSRF guard only
    // trusts loopback. Rewrite same-origin Origin (matches Host) so the
    // proxied request is accepted; leave cross-site Origin unchanged.
    configure(proxy: {
      on: (
        event: "proxyReq",
        listener: (
          proxyReq: { setHeader: (name: string, value: string) => void },
          req: { headers: { origin?: string; host?: string } },
        ) => void,
      ) => void;
    }) {
      proxy.on("proxyReq", (proxyReq, req) => {
        const origin = req.headers.origin;
        const host = req.headers.host;
        if (origin && host && origin === `http://${host}`) {
          proxyReq.setHeader("Origin", `http://127.0.0.1:${devPort}`);
        }
      });
    },
  };
  const apiProxyWithHtmlFallback = {
    ...apiProxy,
    bypass(req: { headers: { accept?: string } }) {
      if (req.headers.accept?.includes("text/html")) {
        return "/index.html";
      }
    },
  };

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(import.meta.dirname, "./src") },
    },
    server: {
      host: true,
      port: devPort,
      allowedHosts: true,
      proxy: {
        ...Object.fromEntries(PROXY_PATHS.map((p) => [p, apiProxy])),
        // SPA RunDetail page — only the two-segment ``/runs/{id}``
        // form should fall back to ``index.html`` on browser navigation.
        // ``/runs/{id}/code`` and ``/runs/{id}/pine`` are API-only and
        // must keep proxying to the backend even when Accept is text/html.
        "^/runs/[^/]+/?$": apiProxyWithHtmlFallback,
        "/runs": apiProxy,
        "/correlation": apiProxyWithHtmlFallback,
        // /options is both the SPA Options Lab route and an API prefix
        // (/options/payoff, /options/chain) — same dual role as /correlation.
        // Overrides the plain PROXY_PATHS entry above.
        "/options": apiProxyWithHtmlFallback,
        "^/alpha(?:/|$)": apiProxy,
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: (id: string) => {
            if (/node_modules\/(react|react-dom|react-router)\//.test(id)) return "vendor-react";
            if (/node_modules\/echarts\//.test(id)) return "vendor-charts";
            return undefined;
          },
        },
      },
    },
  };
});
