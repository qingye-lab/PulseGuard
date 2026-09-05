/// <reference types="vite/client" />

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api.js";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker.js?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker.js?worker";
import "monaco-editor/esm/vs/language/json/monaco.contribution.js";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution.js";

// This module belongs exclusively to the lazy editor boundary. Keep its runtime,
// language support, CSS and workers out of route/list entry imports.
self.MonacoEnvironment = {
  getWorker(_workerId, label) {
    return label === "json" ? new JsonWorker() : new EditorWorker();
  }
};

// Supply the bundled ESM instance before Editor mounts: the loader must never
// fall back to its CDN/AMD default in local or offline LAN installations.
loader.config({ monaco });
