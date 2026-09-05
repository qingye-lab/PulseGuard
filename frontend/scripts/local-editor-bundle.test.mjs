import assert from "node:assert/strict";
import test from "node:test";
import { assertLocalEditorBundle } from "./local-editor-bundle.mjs";

function fixture() {
  return {
    "editor.js": {
      type: "chunk", fileName: "editor.js",
      modules: { "/frontend/src/components/localMonaco.ts": {} },
      code: 'new Worker("/assets/editor.worker-a.js"); new Worker("/assets/json.worker-b.js")'
    },
    "editor.worker-a.js": { type: "asset", fileName: "assets/editor.worker-a.js" },
    "json.worker-b.js": { type: "asset", fileName: "assets/json.worker-b.js" }
  };
}

test("local editor ships both workers referenced by its lazy runtime", () => {
  assert.deepEqual(assertLocalEditorBundle(fixture()), { editor: "assets/editor.worker-a.js", json: "assets/json.worker-b.js" });
});

test("a missing JSON worker cannot silently fall back to the CDN", () => {
  const bundle = fixture();
  delete bundle["json.worker-b.js"];
  assert.throws(() => assertLocalEditorBundle(bundle), /Missing bundled Monaco json worker/);
});

test("orphaned workers do not prove local editor loading", () => {
  const bundle = fixture();
  bundle["editor.js"].code = "loader.init()";
  assert.throws(() => assertLocalEditorBundle(bundle), /not referenced by its local loader/);
});

test("unneeded full language workers fail the editor boundary", () => {
  const bundle = fixture();
  bundle["ts.worker-c.js"] = { type: "asset", fileName: "assets/ts.worker-c.js" };
  assert.throws(() => assertLocalEditorBundle(bundle), /Unneeded Monaco language worker/);
});

test("matching CDN worker filenames cannot masquerade as same-origin assets", () => {
  const bundle = fixture();
  bundle["editor.js"].code = bundle["editor.js"].code.replaceAll('/assets/', 'https://cdn.example/assets/');
  assert.throws(() => assertLocalEditorBundle(bundle), /not referenced by its local loader/);
});
