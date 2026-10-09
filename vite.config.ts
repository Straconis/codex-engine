import { readFileSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";

const packageJson = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf-8"));

// The built app may only run its own scripts and talk to the local backend (any port: the
// desktop app moves it off 8787 when that's taken). Build only: the dev server injects
// inline scripts for hot reload.
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self' http://127.0.0.1:* http://localhost:*",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "codex-engine-csp",
    apply: "build",
    transformIndexHtml: () => [
      { tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: CONTENT_SECURITY_POLICY }, injectTo: "head-prepend" },
    ],
  };
}

export default defineConfig({
  base: "./",
  define: {
    "import.meta.env.PACKAGE_VERSION": JSON.stringify(packageJson.version),
  },
  plugins: [react(), contentSecurityPolicy()],
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    // Build outputs and the Python backend aren't frontend sources. Watching them made the
    // dev server crash (EBUSY) when an installer was being written during a build.
    watch: { ignored: ["**/release/**", "**/dist/**", "**/backend/**", "**/resources/**"] },
  },
});
