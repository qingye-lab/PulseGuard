from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from typing import Any
from urllib.parse import urlsplit

import websockets

from .config import RELAY_HELLO_TIMEOUT_SECONDS, RELAY_MAX_BODY_BYTES, RELAY_MAX_FRAME_BYTES, RELAY_STREAM_IDLE_TIMEOUT_SECONDS
from .relay_tunnel import TunnelStream, decode_data_message, insecure_fingerprint_context, json_dumps, json_loads, pump_reader_to_sender

logger = logging.getLogger(__name__)
WORKER_CONNECT_TIMEOUT_SECONDS = 5


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


async def _send_json(websocket: Any, lock: asyncio.Lock, message: dict[str, Any]) -> None:
    async with lock:
        await websocket.send(json_dumps(message))


async def _send_json_best_effort(websocket: Any, lock: asyncio.Lock, message: dict[str, Any]) -> None:
    try:
        await _send_json(websocket, lock, message)
    except Exception:
        pass


def _worker_endpoint() -> tuple[str, int]:
    url = _env("PULSEGUARD_WORKER_URL", "http://pulseguard-worker:8788")
    parts = urlsplit(url)
    if parts.scheme not in {"http", ""}:
        raise RuntimeError("PULSEGUARD_WORKER_URL only supports http in relay mode")
    host = parts.hostname or "pulseguard-worker"
    try:
        port = parts.port or 8788
    except ValueError as exc:
        raise RuntimeError("PULSEGUARD_WORKER_URL port is invalid") from exc
    return host, port


def _relay_token_version() -> int:
    value = _env("PULSEGUARD_RELAY_TOKEN_VERSION", "1")
    try:
        token_version = int(value)
    except ValueError as exc:
        raise RuntimeError("PULSEGUARD_RELAY_TOKEN_VERSION must be an integer") from exc
    if token_version < 1:
        raise RuntimeError("PULSEGUARD_RELAY_TOKEN_VERSION must be positive")
    return token_version


def _assert_fingerprint(websocket: Any, expected: str) -> None:
    normalized = expected.lower().replace(":", "")
    if not normalized:
        raise RuntimeError("PULSEGUARD_RELAY_FINGERPRINT is required")
    ssl_object = websocket.transport.get_extra_info("ssl_object")
    cert = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    if not cert:
        raise RuntimeError("relay server TLS certificate is unavailable")
    actual = hashlib.sha256(cert).hexdigest()
    if actual != normalized:
        raise RuntimeError("relay server fingerprint mismatch")


async def _heartbeat(websocket: Any, send_lock: asyncio.Lock) -> None:
    while True:
        await asyncio.sleep(15)
        await _send_json(websocket, send_lock, {"type": "heartbeat"})


async def _pump_worker_to_relay(
    stream_id: str,
    reader: asyncio.StreamReader,
    websocket: Any,
    send_lock: asyncio.Lock,
    streams: dict[str, TunnelStream],
) -> None:
    try:
        try:
            await pump_reader_to_sender(
                stream_id,
                reader,
                lambda message: _send_json(websocket, send_lock, message),
                max_bytes=RELAY_MAX_BODY_BYTES,
                idle_timeout_seconds=RELAY_STREAM_IDLE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("relay_worker_stream_failed stream_id=%r error_type=%s", stream_id, type(exc).__name__)
    finally:
        await _close_stream_best_effort(streams, stream_id)


async def _open_worker_stream(stream_id: str, websocket: Any, send_lock: asyncio.Lock, streams: dict[str, TunnelStream]) -> None:
    if stream_id in streams:
        raise RuntimeError("invalid relay message")
    host, port = _worker_endpoint()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=WORKER_CONNECT_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("relay_worker_connect_failed stream_id=%r error_type=%s", stream_id, type(exc).__name__)
        await _send_json_best_effort(websocket, send_lock, {"type": "error", "stream_id": stream_id, "reason": "worker_connect_failed", "error_type": type(exc).__name__})
        return
    stream = TunnelStream(reader=reader, writer=writer)
    streams[stream_id] = stream
    task = asyncio.create_task(_pump_worker_to_relay(stream_id, reader, websocket, send_lock, streams))
    stream.tasks.add(task)


async def _handle_relay_data_message(stream_id: str, message: dict[str, Any], streams: dict[str, TunnelStream]) -> None:
    data = decode_data_message(message)
    stream = streams.get(stream_id)
    if not stream:
        return
    if not stream.record_received(len(data), RELAY_MAX_BODY_BYTES):
        logger.warning("relay_worker_stream_failed stream_id=%r reason=byte_limit", stream_id)
        await _close_stream_best_effort(streams, stream_id)
        return
    try:
        stream.writer.write(data)
        await asyncio.wait_for(stream.writer.drain(), timeout=RELAY_STREAM_IDLE_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("relay_worker_stream_failed stream_id=%r error_type=%s", stream_id, type(exc).__name__)
        await _close_stream_best_effort(streams, stream_id)


async def _close_stream_best_effort(streams: dict[str, TunnelStream], stream_id: str) -> None:
    stream = streams.pop(stream_id, None)
    if not stream:
        return
    try:
        await stream.close()
    except Exception:
        pass


async def _close_all_streams_best_effort(streams: dict[str, TunnelStream]) -> None:
    for stream_id in list(streams):
        await _close_stream_best_effort(streams, stream_id)


async def _handle_relay_message(
    message: dict[str, Any],
    websocket: Any,
    send_lock: asyncio.Lock,
    streams: dict[str, TunnelStream],
) -> None:
    message_type = str(message.get("type") or "")
    stream_id = str(message.get("stream_id") or "")
    if message_type == "heartbeat_ack":
        return
    if message_type == "open" and stream_id:
        await _open_worker_stream(stream_id, websocket, send_lock, streams)
    elif message_type == "data" and stream_id:
        await _handle_relay_data_message(stream_id, message, streams)
    elif message_type in {"close", "error"} and stream_id:
        await _close_stream_best_effort(streams, stream_id)
    else:
        raise RuntimeError("invalid relay message")


async def run_once() -> None:
    relay_url = _env("PULSEGUARD_RELAY_URL")
    runner_id = _env("PULSEGUARD_RUNNER_ID")
    relay_token = _env("PULSEGUARD_RELAY_TOKEN")
    fingerprint = _env("PULSEGUARD_RELAY_FINGERPRINT")
    token_version = _relay_token_version()
    if not relay_url or not runner_id or not relay_token:
        raise RuntimeError("PULSEGUARD_RELAY_URL, PULSEGUARD_RUNNER_ID, and PULSEGUARD_RELAY_TOKEN are required")
    if urlsplit(relay_url).scheme != "wss":
        raise RuntimeError("PULSEGUARD_RELAY_URL must use wss:// in relay mode")
    send_lock = asyncio.Lock()
    streams: dict[str, TunnelStream] = {}
    async with websockets.connect(
        relay_url,
        ssl=insecure_fingerprint_context(),
        max_size=RELAY_MAX_FRAME_BYTES,
        compression=None,
    ) as websocket:
        _assert_fingerprint(websocket, fingerprint)
        await _send_json(
            websocket,
            send_lock,
            {"type": "hello", "runner_id": runner_id, "relay_token": relay_token, "relay_token_version": token_version},
        )
        ready = json_loads(await asyncio.wait_for(websocket.recv(), timeout=RELAY_HELLO_TIMEOUT_SECONDS))
        if ready.get("type") != "ready":
            raise RuntimeError("relay server did not accept the connection")
        logger.info("relay_connected runner_id=%r", runner_id)
        heartbeat = asyncio.create_task(_heartbeat(websocket, send_lock))
        try:
            async for raw in websocket:
                message = json_loads(raw)
                await _handle_relay_message(message, websocket, send_lock, streams)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await _close_all_streams_best_effort(streams)


async def main_loop() -> None:
    delay = 1.0
    while True:
        started = time.monotonic()
        try:
            await run_once()
        except Exception as exc:
            logger.warning("relay_disconnected runner_id=%r error_type=%s", _env("PULSEGUARD_RUNNER_ID"), type(exc).__name__)
        else:
            logger.info("relay_disconnected runner_id=%r reason=connection_closed", _env("PULSEGUARD_RUNNER_ID"))
        if time.monotonic() - started >= 60:
            delay = 1.0
        # Also back off on a clean close; otherwise rejected/closed sessions can spin.
        await asyncio.sleep(random.uniform(delay * 0.8, delay))
        delay = min(delay * 2, 30.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
