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

test("API preview error retains bounded history and timing diagnostics", async () => {
  const original = globalThis.fetch;
  const diagnostics = {
    stage: "script",
    status: "failed",
    message: "脚本未产生 HTTP 响应",
    error_type: "RunFailure",
    url_variables: {},
    variables: {},
    request: { method: "GET", url: "https://example.test" },
    response: null,
    http_requests: [{ request: { method: "GET", url: "https://example.test" }, response: null, duration_ms: 12 }],
    timings: { request_ms: 12, task_ms: 19 },
    logs: ""
  };
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: "接口请求失败：脚本未产生 HTTP 响应", diagnostics }), { status: 400 });
    await assert.rejects(api.inspectApi({
      type: "api",
      entry_url: "https://example.test",
      method: "GET",
      headers_json: "{}",
      body: "",
      script: "async def check(ctx): pass",
      timeout_ms: 10000
    }), error => {
      assert.ok(error instanceof ApiError);
      assert.deepEqual(error.diagnostics, diagnostics);
      return true;
    });
  } finally { globalThis.fetch = original; }
});
