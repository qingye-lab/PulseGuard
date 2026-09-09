import { gzipSync } from "node:zlib";

export const EDITOR_BUDGET = { bytes: 4_450_000, gzipBytes: 1_200_000 };

export function collectEditorReport(bundle, workerFiles) {
  const chunks = Object.values(bundle).filter((item) => item.type === "chunk");
  const entry = chunks.find((item) => item.isEntry);
  const editor = chunks.find((item) => Object.keys(item.modules || {}).some((id) =>
    id.replaceAll("\\", "/").endsWith("/src/components/localMonaco.ts")));
  if (!entry || !editor) throw new Error("Missing editor budget boundary");
  const baseline = new Set();
  const files = new Set();
  const visit = (file, target, dynamic) => {
    if (target.has(file) || (target === files && baseline.has(file))) return;
    const item = bundle[file];
    if (!item) throw new Error(`Missing editor dependency: ${file}`);
    target.add(file);
    if (item.type !== "chunk") return;
    item.imports?.forEach((dependency) => visit(dependency, target, dynamic));
    item.viteMetadata?.importedCss?.forEach((dependency) => visit(dependency, target, dynamic));
    item.viteMetadata?.importedAssets?.forEach((dependency) => visit(dependency, target, dynamic));
    if (dynamic) item.dynamicImports?.forEach((dependency) => visit(dependency, target, dynamic));
  };
  // Count the extra cost of opening an editor, including languages, workers and
  // fonts. Moving code into another chunk must not hide it from this budget.
  visit(entry.fileName, baseline, false);
  visit(editor.fileName, files, true);
  workerFiles.forEach((file) => visit(file, files, true));
  let bytes = 0;
  let gzipBytes = 0;
  for (const file of files) {
    const item = bundle[file];
    const data = Buffer.from(item.type === "chunk" ? item.code : item.source);
    bytes += data.length;
    gzipBytes += gzipSync(data).length;
  }
  if (bytes > EDITOR_BUDGET.bytes || gzipBytes > EDITOR_BUDGET.gzipBytes) {
    throw new Error(`Editor budget exceeded: ${bytes} raw / ${gzipBytes} gzip bytes`);
  }
  return { files: [...files].sort(), bytes, gzipBytes, budget: EDITOR_BUDGET };
}
