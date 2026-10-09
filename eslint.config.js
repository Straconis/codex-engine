// ESLint 9 flat config: the React/TypeScript frontend (src/) and the Electron shell (electron/*.cjs).
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

export default tseslint.config(
  {
    // Build output, dependencies, packaged apps and the Python backend aren't linted.
    ignores: ["dist/**", "release/**", "resources/**", "node_modules/**", "backend/**", "src-tauri/**"],
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended, reactHooks.configs.flat.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: globals.browser,
    },
  },
  {
    files: ["*.config.{js,ts}", "vite.config.ts"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { globals: globals.node },
  },
  {
    // Electron main process and preload scripts: CommonJS running in Node.
    files: ["electron/**/*.cjs"],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "commonjs",
      globals: { ...globals.node },
    },
  },
  {
    // Preload scripts run in the renderer, so they also see the page's window.
    files: ["electron/*preload.cjs"],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
);
