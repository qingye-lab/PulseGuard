from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.app import relay_server, relay_tunnel
from backend.app.relay_tunnel import TunnelStream


class RelayResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        relay_server.ACTIVE_SESSIONS.clear()
        relay_server.PUBLIC_CONNECTIONS = 0

    async def test_database_write_wait_does_not_block_other_relay_connections(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        session = AsyncMock()
        relay_server.ACTIVE_SESSIONS["edge-1"] = session

        def blocked_write(*_args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test storage wait timed out")

        timer = threading.Timer(0.5, release.set)
        timer.start()
        try:
            with patch.object(relay_server.storage, "mark_probe_runner_relay_disconnected", side_effect=blocked_write):
                task = asyncio.create_task(relay_server.revoke_relay_session("edge-1"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertFalse(release.is_set(), "SQLite wait blocked the event loop until the watchdog released it")
                    self.assertTrue(await relay_server._acquire_public_connection())
                finally:
                    release.set()
                    await task
        finally:
            release.set()
            timer.cancel()
        session.close.assert_awaited_once()

    async def test_cancelling_disconnect_waits_for_database_write_before_closing_session(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        events: list[str] = []
        session = AsyncMock()

        async def close():
            events.append("closed")

        session.close.side_effect = close
        relay_server.ACTIVE_SESSIONS["edge-1"] = session

        def write(*_args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test storage wait timed out")
            events.append("persisted")

        with patch.object(relay_server.storage, "mark_probe_runner_relay_disconnected", side_effect=write):
            task = asyncio.create_task(relay_server.revoke_relay_session("edge-1"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertEqual(events, [])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(events, ["persisted", "closed"])

    async def test_session_close_releases_active_tcp_clients_before_waiting_for_server(self) -> None:
        accepted = asyncio.get_running_loop().create_future()

        async def handler(reader, writer):
            accepted.set_result((reader, writer))

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addCleanup(server.close)
        _reader, client = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        self.addCleanup(client.close)
        reader, writer = await accepted
        self.addCleanup(writer.close)
        session = relay_server.RelaySession("edge-1", "token", 1, AsyncMock(), server)
        session.streams["s1"] = TunnelStream(reader, writer)

        await asyncio.wait_for(session.close(), timeout=0.5)

        self.assertTrue(writer.is_closing())
        self.assertEqual(session.streams, {})
        session.websocket.close.assert_awaited_once()

    async def test_outbound_backpressure_obeys_stream_idle_timeout(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"payload")
        messages = []

        async def send(message):
            if message["type"] == "data":
                await asyncio.Event().wait()
            messages.append(message)

        await asyncio.wait_for(
            relay_tunnel.pump_reader_to_sender("s1", reader, send, idle_timeout_seconds=0.01), timeout=0.5,
        )
        self.assertEqual(messages, [{"type": "close", "stream_id": "s1", "reason": "idle_timeout"}])

    async def test_connection_authenticated_during_session_close_cannot_add_a_stream(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        server = Mock()
        server.wait_closed = AsyncMock()
        session = relay_server.RelaySession("edge-1", "token", 1, AsyncMock(), server)
        writer = Mock()
        writer.wait_closed = AsyncMock()

        def verify(*_args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test storage wait timed out")
            return {"ok": True}

        with patch.object(relay_server.storage, "verify_probe_runner_relay_token", side_effect=verify), patch.object(
            relay_server, "_max_concurrent_streams", return_value=2
        ):
            task = asyncio.create_task(relay_server._tcp_client_connected(session, AsyncMock(), writer))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await session.close()
            finally:
                release.set()
                await task
        self.assertEqual(session.streams, {})
        writer.close.assert_called_once()
        session.websocket.send_text.assert_not_awaited()

    async def test_cancelled_tcp_authentication_closes_unregistered_writer(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        session = relay_server.RelaySession("edge-1", "token", 1, AsyncMock(), Mock())
        writer = Mock()
        writer.wait_closed = AsyncMock()

        def verify(*_args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test storage wait timed out")
            return {"ok": True}

        with patch.object(relay_server.storage, "verify_probe_runner_relay_token", side_effect=verify):
            task = asyncio.create_task(relay_server._tcp_client_connected(session, AsyncMock(), writer))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        writer.close.assert_called_once()
        self.assertEqual(session.streams, {})

    async def test_stalled_stream_close_aborts_transport_within_cleanup_budget(self) -> None:
        writer = Mock()
        async def blocked_close():
            await asyncio.Event().wait()

        writer.wait_closed.side_effect = blocked_close
        stream = TunnelStream(AsyncMock(), writer)
        with patch.object(relay_tunnel, "STREAM_CLOSE_TIMEOUT_SECONDS", 0.01, create=True):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(stream.close(), timeout=0.2)
        writer.transport.abort.assert_called_once()

    async def test_repeated_cancellation_of_session_close_still_releases_all_resources(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        server = Mock()
        server.wait_closed = AsyncMock()
        session = relay_server.RelaySession("edge-1", "token", 1, AsyncMock(), server)
        stream = AsyncMock()

        async def blocked_close():
            entered.set()
            await release.wait()

        stream.close.side_effect = blocked_close
        session.streams["s1"] = stream
        task = asyncio.create_task(session.close())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        server.wait_closed.assert_awaited_once()
        session.websocket.close.assert_awaited_once()
        self.assertEqual(session.streams, {})


if __name__ == "__main__":
    unittest.main()
