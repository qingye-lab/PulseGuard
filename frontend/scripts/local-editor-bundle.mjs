// Check production output, not source import spelling: workers must be shipped
// by this build, and the editor must reference those local assets.
export function assertLocalEditorBundle(bundle) {
  const chunks = Object.values(bundle).filter((item) => item.type === "chunk");
  const owners = chunks.filter((item) => Object.keys(item.modules).some((id) => id.replaceAll("\\", "/").endsWith("/src/components/localMonaco.ts")));
  if (owners.length !== 1) throw new Error("Missing local Monaco loader boundary");
  const editor = owners[0];
  // Vite's ?worker transform emits root-relative Worker URLs in production;
  // it does not retain the source module's import.meta.url spelling.
  const workerUrls = [...editor.code.matchAll(/\bnew Worker\(\s*["']([^"']+)["']/g)].map((match) => match[1]);
  const workers = {};
  for (const label of ["editor", "json"]) {
    const worker = Object.values(bundle).find((item) => new RegExp(`(?:^|/)${label}\\.worker-[^/]+\\.js$`).test(item.fileName));
    if (!worker) throw new Error(`Missing bundled Monaco ${label} worker`);
    const basename = worker.fileName.split("/").at(-1);
    if (!workerUrls.some((url) => /^(?!\/\/)(?:\/|\.\.?\/)/.test(url) && url.endsWith(`/${basename}`))) {
      throw new Error(`Monaco ${label} worker is not referenced by its local loader`);
    }
    workers[label] = worker.fileName;
  }
  const unsupportedWorker = Object.values(bundle).find((item) => /(?:^|\/)(?:ts|css|html)\.worker-[^/]+\.js$/.test(item.fileName));
  if (unsupportedWorker) throw new Error(`Unneeded Monaco language worker: ${unsupportedWorker.fileName}`);
  return workers;
}
