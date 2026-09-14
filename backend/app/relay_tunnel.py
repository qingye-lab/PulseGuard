from __future__ import annotations

import asyncio
import base64
import binascii
import json
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


JsonSender = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class TunnelStream:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    received_bytes: int = 0
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def record_received(self, byte_count: int, max_bytes: int | None = None) -> bool:
        self.received_bytes += max(0, int(byte_count))
        return max_bytes is None or self.received_bytes <= max_bytes

    async def close(self) -> None:
        current_task = asyncio.current_task()
        wait_tasks = [task for task in list(self.tasks) if task is not current_task]
        for task in wait_tasks:
            task.cancel()
        try:
            self.writer.close()
            await self.writer.wait_closed()
        finally:
            if wait_tasks:
                await asyncio.gather(*wait_tasks, return_exceptions=True)
                self.tasks.difference_update(wait_tasks)


async def pump_reader_to_sender(
    stream_id: str,
    reader: asyncio.StreamReader,
    send_json: JsonSender,
    max_bytes: int | None = None,
    idle_timeout_seconds: float | None = None,
) -> None:
    sent_bytes = 0
    close_reason = "eof"
    try:
        while True:
            if idle_timeout_seconds is None:
                chunk = await reader.read(65536)
            else:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=idle_timeout_seconds)
                except TimeoutError:
                    close_reason = "idle_timeout"
                    break
            if not chunk:
                if sent_bytes == 0:
                    close_reason = "eof_before_data"
                break
            sent_bytes += len(chunk)
            if max_bytes is not None and sent_bytes > max_bytes:
                close_reason = "byte_limit"
                break
            await send_json({"type": "data", "stream_id": stream_id, "data": base64.b64encode(chunk).decode("ascii")})
    except asyncio.CancelledError:
        close_reason = "cancelled"
        raise
    except Exception:
        close_reason = "io_error"
        raise
    finally:
        await send_json({"type": "close", "stream_id": stream_id, "reason": close_reason})


def decode_data_message(message: dict[str, Any]) -> bytes:
    try:
        return base64.b64decode(str(message.get("data") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("relay data frame is not valid base64") from exc


def json_dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


def json_loads(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("relay message must be an object")
    return value


def insecure_fingerprint_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context
