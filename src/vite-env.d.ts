/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Build-time override of the backend address (default http://127.0.0.1:8787). */
  readonly VITE_CODEX_ENGINE_API?: string;
  /** package.json "version", set by vite.config.ts (`define`). */
  readonly PACKAGE_VERSION: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
