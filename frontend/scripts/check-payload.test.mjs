import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";

const { outputFiles } = buildSync({
  entryPoints: [fileURLToPath(new URL("../src/checkPayload.ts", import.meta.url))],
  bundle: true, write: false, platform: "node", format: "esm"
});
const { prepareCheckPayload, sameCheckPayload } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
const check = {
  name: "Dynamic", type: "ui", enabled: false, entry_url: "https://example/?id=${id}",
  viewport_mode: "h5", interval_seconds: 300, timeout_ms: 10000,
  headers_json: "{}", body: "", script: "", setup_script: "async def setup(ctx, page):\n    ctx.vars.set('id', 1)",
  assertions_json: '[{"id": "name", "enabled": true, "type": "text_present", "expected_text": "${name}"}]'
};

test("successful UI debug does not become stale after JSON serialization", () => {
  assert.equal(sameCheckPayload(check, prepareCheckPayload(check)), true);
});

test("changing a dynamic assertion still invalidates the previous debug result", () => {
  const changed = { ...check, assertions_json: check.assertions_json.replace("${name}", "${city}") };
  assert.equal(sameCheckPayload(changed, prepareCheckPayload(check)), false);
});

test("script changes and invalid drafts remain distinguishable", () => {
  assert.equal(sameCheckPayload(check, { ...check, setup_script: check.setup_script + "\n    ctx.vars.set('id', 2)" }), false);
  assert.equal(sameCheckPayload({ ...check, assertions_json: "[" }, { ...check, assertions_json: "{" }), false);
});
