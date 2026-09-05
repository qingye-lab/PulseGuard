import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";

const { outputFiles } = buildSync({ entryPoints: [fileURLToPath(new URL("../src/api.ts", import.meta.url))], bundle: true, write: false, platform: "node", format: "esm" });
const { api, ApiError } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);

test("UI error retains structured evidence without breaking legacy detail", async () => {
  const original = globalThis.fetch;
  try {
    for (const diagnostics of [undefined, { stage: "setup", status: "failed", variables: {} }]) {
      globalThis.fetch = async () => new Response(JSON.stringify({ detail: "source empty", diagnostics }), { status: 400 });
      await assert.rejects(api.inspectUi({ type: "ui", entry_url: "https://example.test" }), error => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.message, "source empty");
        assert.deepEqual(error.diagnostics, diagnostics);
        return true;
      });
    }
    globalThis.fetch = async () => new Response("unavailable", { status: 503 });
    await assert.rejects(api.inspectUi({}), /503/);
  } finally { globalThis.fetch = original; }
});
