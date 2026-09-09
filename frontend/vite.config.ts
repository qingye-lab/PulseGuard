import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { bundleBudget } from "./scripts/bundle-budget.mjs";
import { monacoModuleSideEffects } from "./scripts/monaco-features.mjs";
import { precompressedAssets } from "./scripts/precompressed-assets.mjs";

const devApiTarget = process.env.VITE_DEV_API_TARGET || "http://127.0.0.1:8787";
const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react(), tailwindcss(), bundleBudget(), precompressedAssets()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src")
    }
  },
  build: {
    // Let Rollup retain route-level AntD boundaries. A package-wide manual
    // chunk forces date pickers, tables and editors onto every initial route.
    manifest: true,
    rollupOptions: {
      treeshake: { moduleSideEffects: monacoModuleSideEffects }
    }
  },
  server: {
    proxy: {
      "^/api(/|$)": devApiTarget,
      "^/artifacts(/|$)": devApiTarget
    }
  }
});
