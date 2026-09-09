import { gzipSync } from "node:zlib";

// Compress final output once, including Vite workers. The original remains
// available for clients that do not accept gzip and for byte-range requests.
export function precompressedAssets() {
  return {
    name: "pulseguard-precompressed-assets",
    apply: "build",
    generateBundle: {
      order: "post",
      handler(_options, bundle) {
        for (const item of Object.values(bundle)) {
          if (!/^assets\/.*\.(?:js|css|svg|ttf)$/.test(item.fileName)) continue;
          const data = Buffer.from(item.type === "chunk" ? item.code : item.source);
          if (data.length < 1024) continue;
          const compressed = gzipSync(data, { level: 9 });
          if (compressed.length >= data.length) continue;
          this.emitFile({ type: "asset", fileName: `${item.fileName}.gz`, source: compressed });
        }
      }
    }
  };
}
