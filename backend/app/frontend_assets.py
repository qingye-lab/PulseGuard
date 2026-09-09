"""Vite asset delivery with build-time gzip and content-hash caching."""

from __future__ import annotations

import mimetypes
import re

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


HASHED_ASSET = re.compile(r"-[A-Za-z0-9_-]{8,}\.(?:js|css|svg|ttf|woff2?|png|jpe?g|webp|ico)$")


def _accepts_gzip(value: str) -> bool:
    qualities: dict[str, float] = {}
    for entry in value.lower().split(","):
        name, *parameters = entry.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            key, separator, raw = parameter.strip().partition("=")
            if key.strip() == "q" and separator:
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
        qualities[name] = quality if 0 <= quality <= 1 else 0.0
    return qualities.get("gzip", qualities.get("*", 0.0)) > 0


class FrontendAssets(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        # Resolve the original first, preserving StaticFiles' method, path and
        # missing-file checks. Each representation keeps its own ETag/length.
        response = await super().get_response(path, scope)
        request_headers = Headers(scope=scope)
        if "range" not in request_headers and _accepts_gzip(request_headers.get("accept-encoding", "")):
            try:
                compressed = await super().get_response(f"{path}.gz", scope)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            else:
                response = compressed
                if response.status_code != 304:
                    response.headers["Content-Encoding"] = "gzip"
                    response.headers["Content-Type"] = mimetypes.guess_type(path)[0] or "application/octet-stream"
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
            if HASHED_ASSET.search(path)
            else "no-cache, must-revalidate"
        )
        return response
