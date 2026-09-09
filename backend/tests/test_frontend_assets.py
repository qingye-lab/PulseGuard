from __future__ import annotations

import gzip
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.frontend_assets import FrontendAssets


class FrontendAssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.body = b"console.log('ready');\n" * 500
        self.name = "CodeEditorPanel-abcdefgh.js"
        (self.root / self.name).write_bytes(self.body)
        self.compressed = gzip.compress(self.body, mtime=0)
        (self.root / f"{self.name}.gz").write_bytes(self.compressed)
        (self.root / "config.js").write_bytes(self.body)
        application = FastAPI()
        application.mount("/assets", FrontendAssets(directory=self.root))
        self.client = TestClient(application)
        self.addCleanup(self.client.close)
        self.url = f"/assets/{self.name}"

    def test_gzip_transfers_compressed_bytes_with_original_javascript_type(self):
        response = self.client.get(self.url, headers={"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.body)  # httpx automatically decompresses.
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertIn("javascript", response.headers["content-type"])
        self.assertEqual(int(response.headers["content-length"]), len(self.compressed))
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertEqual(response.headers["vary"], "Accept-Encoding")

    def test_identity_and_explicit_gzip_refusal_preserve_original(self):
        for encoding in ("", "identity", "br", "gzip;q=0, *;q=1", "gzip;q=invalid"):
            with self.subTest(encoding=encoding):
                response = self.client.get(self.url, headers={"Accept-Encoding": encoding})
                self.assertNotIn("content-encoding", response.headers)
                self.assertEqual(response.content, self.body)
                self.assertEqual(response.headers["vary"], "Accept-Encoding")

    def test_head_and_conditional_get_use_selected_representation(self):
        headers = {"Accept-Encoding": "gzip"}
        head = self.client.head(self.url, headers=headers)
        self.assertEqual(head.content, b"")
        self.assertEqual(int(head.headers["content-length"]), len(self.compressed))
        response = self.client.get(self.url, headers={**headers, "If-None-Match": head.headers["etag"]})
        self.assertEqual(response.status_code, 304)
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertEqual(response.headers["vary"], "Accept-Encoding")
        identity = self.client.get(self.url, headers={"Accept-Encoding": "identity", "If-None-Match": head.headers["etag"]})
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.content, self.body)

    def test_missing_compressed_file_and_unhashed_file_remain_usable(self):
        (self.root / f"{self.name}.gz").unlink()
        response = self.client.get(self.url)
        self.assertEqual(response.content, self.body)
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(self.client.get("/assets/config.js").headers["cache-control"], "no-cache, must-revalidate")

    def test_ranges_remain_uncompressed(self):
        response = self.client.get(self.url, headers={"Accept-Encoding": "gzip", "Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.body[:10])
        self.assertNotIn("content-encoding", response.headers)

    def test_missing_files_and_invalid_methods_do_not_become_cacheable_successes(self):
        self.assertEqual(self.client.get("/assets/missing-abcdefgh.js").status_code, 404)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertEqual(self.client.get("/assets/%2e%2e/secret.txt").status_code, 404)


if __name__ == "__main__":
    unittest.main()
