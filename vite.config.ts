import { readFileSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const packageJson = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf-8"));

export default defineConfig({
  base: "./",
  define: {
    "import.meta.env.PACKAGE_VERSION": JSON.stringify(packageJson.version),
  },
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    // Build outputs and the Python backend aren't frontend sources. Watching them made the
    // dev server crash (EBUSY) when an installer was being written during a build.
    watch: { ignored: ["**/release/**", "**/dist/**", "**/backend/**", "**/resources/**"] },
  },
});
