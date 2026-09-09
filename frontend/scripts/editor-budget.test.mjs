import assert from "node:assert/strict";
import test from "node:test";
import { collectEditorReport, EDITOR_BUDGET } from "./editor-budget.mjs";

function fixture() {
  const chunk = (fileName, overrides = {}) => ({
    type: "chunk", fileName, code: "editor();", imports: [], dynamicImports: [], ...overrides
  });
  return {
    "entry.js": chunk("entry.js", { isEntry: true, imports: ["shared.js"] }),
    "shared.js": chunk("shared.js"),
    "editor.js": chunk("editor.js", {
      modules: { "/frontend/src/components/localMonaco.ts": {} },
      imports: ["shared.js", "core.js"], dynamicImports: ["json.js"],
      viteMetadata: { importedCss: new Set(["editor.css"]), importedAssets: new Set(["font.ttf"]) }
    }),
    "core.js": chunk("core.js"),
    "json.js": chunk("json.js", { imports: ["core.js"] }),
    "worker.js": { type: "asset", fileName: "worker.js", source: "worker();" },
    "font.ttf": { type: "asset", fileName: "font.ttf", source: Buffer.from("font") },
    "editor.css": { type: "asset", fileName: "editor.css", source: ".editor{}" }
  };
}

test("counts all editor dependencies once, including async languages, fonts and workers", () => {
  const report = collectEditorReport(fixture(), ["worker.js"]);
  assert.deepEqual(report.files, ["core.js", "editor.css", "editor.js", "font.ttf", "json.js", "worker.js"]);
  assert.equal(report.bytes, 3 * "editor();".length + "worker();".length + ".editor{}".length + 4);
});

test("splitting a large editor into an async chunk cannot bypass the raw JS budget", () => {
  const bundle = fixture();
  bundle["json.js"].code = "x".repeat(EDITOR_BUDGET.bytes);
  assert.throws(() => collectEditorReport(bundle, ["worker.js"]), /Editor budget exceeded/);
});

test("a missing language or worker fails the build", () => {
  const bundle = fixture();
  delete bundle["json.js"];
  assert.throws(() => collectEditorReport(bundle, ["worker.js"]), /Missing editor dependency/);
});
