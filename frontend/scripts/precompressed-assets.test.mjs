import assert from "node:assert/strict";
import { gunzipSync } from "node:zlib";
import test from "node:test";
import { precompressedAssets } from "./precompressed-assets.mjs";

test("compressed JS, CSS and emitted workers round-trip the final output", () => {
  const code = "console.log('ready');\n".repeat(100);
  const bundle = {
    js: { type: "chunk", fileName: "assets/editor-abcdefgh.js", code },
    css: { type: "asset", fileName: "assets/editor-abcdefgh.css", source: ".editor{color:red}".repeat(100) },
    worker: { type: "asset", fileName: "assets/editor.worker-abcdefgh.js", source: Buffer.from(code) },
    tiny: { type: "chunk", fileName: "assets/small-abcdefgh.js", code: "1" },
    index: { type: "asset", fileName: "index.html", source: code }
  };
  const emitted = [];
  precompressedAssets().generateBundle.handler.call({ emitFile: (item) => emitted.push(item) }, {}, bundle);
  assert.equal(emitted.length, 3);
  for (const item of emitted) {
    const original = Object.values(bundle).find((entry) => `${entry.fileName}.gz` === item.fileName);
    const bytes = Buffer.from(original.code || original.source);
    assert.deepEqual(gunzipSync(item.source), bytes);
    assert.ok(item.source.length < bytes.length);
  }
});
