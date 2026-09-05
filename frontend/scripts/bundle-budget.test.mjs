import assert from "node:assert/strict";
import test from "node:test";
import { randomBytes } from "node:crypto";
import { collectBundleReport } from "./bundle-budget.mjs";

function fixture() {
  const chunk = (fileName, facadeModuleId, imports = [], modules = {}) => ({
    type: "chunk", fileName, facadeModuleId, imports, modules, code: fileName,
    isEntry: fileName === "entry.js", viteMetadata: { importedCss: new Set() }
  });
  return {
    "entry.js": chunk("entry.js", "/frontend/index.html", ["shared.js"]),
    "overview.js": chunk("overview.js", "/frontend/src/pages/OverviewPage.tsx", ["shared.js"]),
    "monitoring.js": chunk("monitoring.js", "/frontend/src/pages/MonitoringPage.tsx", ["shared.js"]),
    "chart.js": chunk("chart.js", "/frontend/src/components/LatencyChart.tsx", ["shared.js"]),
    "shared.js": chunk("shared.js", null)
  };
}

test("counts shared imports once and includes the primary async chart and CSS", () => {
  const bundle = fixture();
  bundle["shared.css"] = { type: "asset", fileName: "shared.css", source: "body{}" };
  bundle["shared.js"].viteMetadata.importedCss.add("shared.css");
  const report = collectBundleReport(bundle);
  assert.deepEqual(report.routes.entry.files, ["entry.js", "shared.css", "shared.js"]);
  assert.deepEqual(report.routes.monitoring.files, ["chart.js", "entry.js", "monitoring.js", "shared.css", "shared.js"]);
});

test("rejects editor hidden in a shared entry dependency", () => {
  const bundle = fixture();
  bundle["shared.js"].modules["/node_modules/monaco-editor/esm/editor.js"] = { renderedLength: 50 };
  assert.throws(() => collectBundleReport(bundle), /Editor leaked/);
});

test("rejects whole chart registry even in an async chart chunk", () => {
  const bundle = fixture();
  bundle["chart.js"].modules["/node_modules/@antv/g2/esm/lib/std.js"] = { renderedLength: 50 };
  assert.throws(() => collectBundleReport(bundle), /full chart registry/);
});

test("rejects chart runtime pulled into the overview through shared imports", () => {
  const bundle = fixture();
  bundle["shared.js"].modules["/node_modules/@antv/g2/esm/api/runtime.js"] = { renderedLength: 50 };
  assert.throws(() => collectBundleReport(bundle), /Chart runtime leaked/);
});

test("rejects editor CSS even without its JavaScript module", () => {
  const bundle = fixture();
  bundle["CodeEditorPanel.css"] = { type: "asset", fileName: "CodeEditorPanel.css", source: ".monaco-editor{}" };
  bundle["entry.js"].viteMetadata.importedCss.add("CodeEditorPanel.css");
  assert.throws(() => collectBundleReport(bundle), /Editor CSS leaked/);
});

test("rejects budget regression hidden in a shared import", () => {
  const bundle = fixture();
  bundle["shared.js"].code = randomBytes(400_000).toString("base64");
  assert.throws(() => collectBundleReport(bundle), /gzip budget exceeded/);
});
