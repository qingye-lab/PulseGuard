import { gzipSync } from "node:zlib";
import { assertLocalEditorBundle } from "./local-editor-bundle.mjs";

// Gzip budgets cover every shared JS/CSS dependency, not just a route chunk.
// Monitoring includes its primary chart even though that chunk is async: moving
// the same bytes behind import() must not make the budget appear to improve.
export const ROUTE_BUDGETS = {
  entry: 300_000,
  overview: 400_000,
  monitoring: 700_000
};

export function collectBundleReport(bundle) {
  const chunks = Object.values(bundle).filter((item) => item.type === "chunk");
  const find = (suffix) => chunks.find((item) => item.facadeModuleId?.replaceAll("\\", "/").endsWith(suffix));
  const entry = chunks.find((item) => item.isEntry);
  const overview = find("/src/pages/OverviewPage.tsx");
  const monitoring = find("/src/pages/MonitoringPage.tsx");
  const chart = find("/src/components/LatencyChart.tsx");
  if (!entry || !overview || !monitoring || !chart) throw new Error("Missing required bundle boundary");

  const closure = (roots) => {
    const files = new Set();
    const visit = (file) => {
      if (files.has(file)) return;
      const item = bundle[file];
      if (!item) throw new Error(`Missing bundle dependency: ${file}`);
      files.add(file);
      if (item.type !== "chunk") return;
      item.imports.forEach(visit);
      item.viteMetadata?.importedCss?.forEach(visit);
    };
    roots.forEach((item) => visit(item.fileName));
    const modules = [...files].flatMap((file) => {
      const item = bundle[file];
      return item.type === "chunk"
        ? Object.entries(item.modules).filter(([, value]) => value.renderedLength > 0).map(([id]) => id.replaceAll("\\", "/"))
        : [];
    });
    let bytes = 0;
    let gzipBytes = 0;
    files.forEach((file) => {
      const item = bundle[file];
      const source = item.type === "chunk" ? item.code : item.source;
      const data = typeof source === "string" ? Buffer.from(source) : Buffer.from(source);
      bytes += data.length;
      gzipBytes += gzipSync(data).length;
    });
    return { files: [...files].sort(), bytes, gzipBytes, modules };
  };

  const routes = {
    entry: closure([entry]),
    overview: closure([entry, overview]),
    monitoring: closure([entry, monitoring, chart])
  };
  for (const [name, route] of Object.entries(routes)) {
    if (route.gzipBytes > ROUTE_BUDGETS[name]) {
      throw new Error(`${name} gzip budget exceeded: ${route.gzipBytes} > ${ROUTE_BUDGETS[name]} bytes`);
    }
    if (route.modules.some((id) => /\/monaco-editor\/|\/CodeEditorPanel\.tsx$/.test(id))) {
      throw new Error(`Editor leaked into ${name} static dependency closure`);
    }
    if (route.files.some((file) => /(?:CodeEditorPanel|monaco).*\.css$/i.test(file))) {
      throw new Error(`Editor CSS leaked into ${name} static dependency closure`);
    }
    if (name !== "monitoring" && route.modules.some((id) => /\/@antv\/|\/LatencyChart\.tsx$/.test(id))) {
      throw new Error(`Chart runtime leaked into ${name} static dependency closure`);
    }
  }
  const forbiddenChartRegistry = /\/@ant-design\/plots\/|\/@antv\/g2\/esm\/lib\/(?:std|core|geo|graph|plot)\.js$/;
  if (routes.monitoring.modules.some((id) => forbiddenChartRegistry.test(id))) {
    throw new Error("Monitoring must import only its time-series runtime, not a full chart registry");
  }
  return {
    budgets: ROUTE_BUDGETS,
    routes: Object.fromEntries(Object.entries(routes).map(([name, { modules, ...summary }]) => [name, summary]))
  };
}

export function bundleBudget() {
  return {
    name: "pulseguard-bundle-budget",
    enforce: "post",
    generateBundle: {
      // Run after Vite replaces asset URLs and finalizes preload/CSS imports.
      order: "post",
      handler(_options, bundle) {
        const report = collectBundleReport(bundle);
        report.editorWorkers = assertLocalEditorBundle(bundle);
        this.emitFile({ type: "asset", fileName: ".vite/bundle-budget.json", source: JSON.stringify(report, null, 2) });
        console.log("Bundle gzip (including shared JS/CSS): " + Object.entries(report.routes)
          .map(([name, route]) => `${name} ${(route.gzipBytes / 1000).toFixed(1)} kB`).join(", "));
      }
    }
  };
}
