"""Bounded timings for console reads; never collect request or SQL contents."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
from threading import Lock
from time import perf_counter
from typing import Iterator
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger(__name__)
SLOW_REQUEST_MS = 500
TIMED_PATHS = frozenset({
    "/api/health", "/api/projects", "/api/settings", "/api/project-alert-settings",
    "/api/runtime", "/api/checks", "/api/runners", "/api/runs-page",
    "/api/overview", "/api/status-page", "/api/maintenance",
})


@dataclass
class RequestTiming:
    started_at: float = field(default_factory=perf_counter)
    request_id: str = field(default_factory=lambda: uuid4().hex)
    durations: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)
    closed: bool = False

    def add(self, name: str, milliseconds: float) -> None:
        with self.lock:
            if not self.closed:
                self.durations[name] = self.durations.get(name, 0.0) + milliseconds


_current: ContextVar[RequestTiming | None] = ContextVar("request_timing", default=None)


@contextmanager
def phase(name: str) -> Iterator[None]:
    timing = _current.get()
    if timing is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        timing.add(name, (perf_counter() - started) * 1000)


def handler_started() -> None:
    """Includes dependency resolution and thread dispatch, not just queue time."""
    timing = _current.get()
    if timing is not None:
        timing.add("dispatch", (perf_counter() - timing.started_at) * 1000)


def record_count(name: str, value: int) -> None:
    timing = _current.get()
    if timing is not None:
        with timing.lock:
            if not timing.closed:
                timing.counts[name] = value


class RequestTimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET" or scope["path"] not in TIMED_PATHS:
            await self.app(scope, receive, send)
            return
        timing = RequestTiming()
        token = _current.set(timing)
        status = 500

        async def send_timed(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                with timing.lock:
                    durations = dict(timing.durations)
                durations["server"] = (perf_counter() - timing.started_at) * 1000
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = timing.request_id
                headers.append("Server-Timing", ", ".join(
                    f"{name};dur={duration:.2f}" for name, duration in durations.items()
                ))
            await send(message)

        try:
            await self.app(scope, receive, send_timed)
        finally:
            elapsed = (perf_counter() - timing.started_at) * 1000
            with timing.lock:
                timing.closed = True
                durations = {name: round(value, 2) for name, value in timing.durations.items()}
                counts = dict(timing.counts)
            _current.reset(token)
            if elapsed >= SLOW_REQUEST_MS:
                # The path is allowlisted and the ID is generated here. Never log query
                # strings, user-provided IDs, headers, SQL or response payloads.
                logger.warning("slow_api_request %s", json.dumps({
                    "request_id": timing.request_id, "route": scope["path"],
                    "status": status, "duration_ms": round(elapsed, 2),
                    "phases_ms": durations, "counts": counts,
                }, separators=(",", ":")))
