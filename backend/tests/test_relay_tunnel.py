from __future__ import annotations

import asyncio
import base64
import unittest

from backend.app.relay_tunnel import TunnelStream, decode_data_message, pump_reader_to_sender


class FakeReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(0)
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class SlowReader:
    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(1)
        return b"late"


class FakeWriter:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class FailingWriter(FakeWriter):
    async def wait_closed(self) -> None:
        raise RuntimeError("writer close failed")


class FailingCloseWriter(FakeWriter):
    def close(self) -> None:
        raise RuntimeError("writer close failed")


class RelayTunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_pump_reader_to_sender_caps_cumulative_bytes(self) -> None:
        sent: list[dict[str, object]] = []

        async def send_json(message: dict[str, object]) -> None:
            sent.append(message)

        await pump_reader_to_sender("stream-1", FakeReader([b"abc", b"def"]), send_json, max_bytes=5)

        self.assertEqual([message["type"] for message in sent], ["data", "close"])
        self.assertEqual(base64.b64decode(str(sent[0]["data"])), b"abc")
        self.assertEqual(sent[-1]["reason"], "byte_limit")

    async def test_pump_reader_to_sender_closes_idle_stream(self) -> None:
        sent: list[dict[str, object]] = []

        async def send_json(message: dict[str, object]) -> None:
            sent.append(message)

        await pump_reader_to_sender("stream-1", SlowReader(), send_json, idle_timeout_seconds=0.01)  # type: ignore[arg-type]

        self.assertEqual(sent, [{"type": "close", "stream_id": "stream-1", "reason": "idle_timeout"}])

    async def test_tunnel_stream_caps_cumulative_received_bytes(self) -> None:
        stream = TunnelStream(reader=FakeReader([]), writer=FakeWriter())  # type: ignore[arg-type]

        self.assertTrue(stream.record_received(3, max_bytes=5))
        self.assertFalse(stream.record_received(3, max_bytes=5))

    async def test_decode_data_message_rejects_invalid_base64(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid base64"):
            decode_data_message({"data": "not-base64!"})

    async def test_tunnel_stream_close_waits_for_cancelled_tasks(self) -> None:
        task_cancelled = asyncio.Event()

        async def wait_forever() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                task_cancelled.set()

        task = asyncio.create_task(wait_forever())
        stream = TunnelStream(reader=FakeReader([]), writer=FakeWriter())  # type: ignore[arg-type]
        stream.tasks.add(task)
        await asyncio.sleep(0)

        await stream.close()

        self.assertTrue(task.done())
        self.assertTrue(task_cancelled.is_set())
        self.assertNotIn(task, stream.tasks)

    async def test_tunnel_stream_close_waits_for_tasks_after_close_failure(self) -> None:
        task_cancelled = asyncio.Event()

        async def wait_forever() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                task_cancelled.set()

        task = asyncio.create_task(wait_forever())
        stream = TunnelStream(reader=FakeReader([]), writer=FailingCloseWriter())  # type: ignore[arg-type]
        stream.tasks.add(task)
        await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "writer close failed"):
            await stream.close()

        self.assertTrue(task.done())
        self.assertTrue(task_cancelled.is_set())
        self.assertNotIn(task, stream.tasks)

    async def test_tunnel_stream_close_waits_for_tasks_after_writer_failure(self) -> None:
        task_cancelled = asyncio.Event()

        async def wait_forever() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                task_cancelled.set()

        task = asyncio.create_task(wait_forever())
        stream = TunnelStream(reader=FakeReader([]), writer=FailingWriter())  # type: ignore[arg-type]
        stream.tasks.add(task)
        await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "writer close failed"):
            await stream.close()

        self.assertTrue(task.done())
        self.assertTrue(task_cancelled.is_set())
        self.assertNotIn(task, stream.tasks)
