from __future__ import annotations

import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app import relay_server


class FakeServer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.close_args: tuple[object, ...] | None = None
        self.close_kwargs: dict[str, object] | None = None

    async def send_text(self, message: str) -> None:
        self.messages.append(message)

    async def close(self, *args: object, **kwargs: object) -> None:
        self.close_args = args
        self.close_kwargs = kwargs


class FailingCloseWebSocket(FakeWebSocket):
    async def close(self, *args: object, **kwargs: object) -> None:
        await super().close(*args, **kwargs)
        raise RuntimeError("websocket close failed")


class TextWebSocket:
    def __init__(self, text: str) -> None:
        self.text = text

    async def receive_text(self) -> str:
        return self.text


class WaitingWebSocket:
    async def receive_text(self) -> str:
        await asyncio.Event().wait()
        return "{}"


class ConnectWebSocket(TextWebSocket):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.accepted = False
        self.close_kwargs: dict[str, object] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *args: object, **kwargs: object) -> None:
        if self.close_kwargs is None:
            self.close_kwargs = kwargs


class AcceptFailWebSocket:
    async def accept(self) -> None:
        raise RuntimeError("accept failed")


class SessionWebSocket:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.accepted = False
        self.messages: list[str] = []
        self.close_kwargs: dict[str, object] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if not self.texts:
            raise AssertionError("unexpected websocket receive")
        return self.texts.pop(0)

    async def send_text(self, message: str) -> None:
        self.messages.append(message)

    async def close(self, *args: object, **kwargs: object) -> None:
        if self.close_kwargs is None:
            self.close_kwargs = kwargs


class FailingCloseSessionWebSocket(SessionWebSocket):
    async def close(self, *args: object, **kwargs: object) -> None:
        await super().close(*args, **kwargs)
        raise RuntimeError("websocket close failed")


class BlockingReader:
    def __init__(self) -> None:
        self.done = asyncio.Event()

    async def read(self, _size: int) -> bytes:
        await self.done.wait()
        return b""


class EmptyReader:
    async def read(self, _size: int) -> bytes:
        return b""


class FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class SlowDrainWriter(FakeWriter):
    async def drain(self) -> None:
        await asyncio.Event().wait()


class FailingCloseWriter(FakeWriter):
    async def wait_closed(self) -> None:
        raise RuntimeError("writer close failed")


class FailingWriteWriter(FakeWriter):
    def write(self, data: bytes) -> None:
        super().write(data)
        raise RuntimeError("stream write failed")


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeStream:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self.fail:
            raise RuntimeError("stream close failed")


class RelayServerControlTests(unittest.TestCase):
    def tearDown(self) -> None:
        relay_server.ACTIVE_SESSIONS.clear()

    def test_control_revoke_requires_token(self) -> None:
        with patch("backend.app.relay_server.relay_control_token", return_value="pgrc_secret"):
            response = TestClient(relay_server.control_app).post(
                "/relay/control/revoke",
                json={"runner_id": "edge-1"},
                headers={"Authorization": "Bearer wrong"},
            )

        self.assertEqual(response.status_code, 403)

    def test_control_revoke_closes_active_session(self) -> None:
        session = FakeSession()
        relay_server.ACTIVE_SESSIONS["edge-1"] = session  # type: ignore[assignment]

        with patch("backend.app.relay_server.relay_control_token", return_value="pgrc_secret"), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_disconnected"
        ) as mark_disconnected:
            response = TestClient(relay_server.control_app).post(
                "/relay/control/revoke",
                json={"runner_id": "edge-1", "reason": "deployment command regenerated"},
                headers={"Authorization": "Bearer pgrc_secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "revoked": True})
        self.assertTrue(session.closed)
        self.assertNotIn("edge-1", relay_server.ACTIVE_SESSIONS)
        mark_disconnected.assert_called_once_with("edge-1", "deployment command regenerated")

    def test_prepare_runtime_invalidates_stale_relay_sessions(self) -> None:
        with patch("backend.app.relay_server.ensure_runtime_dirs") as ensure_runtime_dirs, patch(
            "backend.app.relay_server.storage.init_db"
        ) as init_db, patch("backend.app.relay_server.ensure_relay_certificate") as ensure_relay_certificate, patch(
            "backend.app.relay_server.relay_control_token"
        ) as relay_control_token, patch(
            "backend.app.relay_server.storage.mark_relay_runners_restarted"
        ) as mark_restarted:
            relay_server._prepare_runtime()

        ensure_runtime_dirs.assert_called_once()
        init_db.assert_called_once()
        ensure_relay_certificate.assert_called_once()
        relay_control_token.assert_called_once()
        mark_restarted.assert_called_once()

    def test_public_config_caps_frames_and_disables_compression(self) -> None:
        with patch("backend.app.relay_server.RELAY_MAX_FRAME_BYTES", 12345):
            config = relay_server._public_config()

        self.assertEqual(config.ws_max_size, 12345)
        self.assertFalse(config.ws_per_message_deflate)


class RelayServerEntryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        relay_server.ACTIVE_SESSIONS.clear()
        relay_server.PUBLIC_CONNECTIONS = 0
        relay_server.AUTH_FAILURES.clear()

    async def test_public_connection_limit_rejects_after_cap(self) -> None:
        with patch("backend.app.relay_server.RELAY_MAX_PUBLIC_CONNECTIONS", 1):
            self.assertTrue(await relay_server._acquire_public_connection())
            self.assertFalse(await relay_server._acquire_public_connection())
            await relay_server._release_public_connection()

        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)

    async def test_public_connection_count_released_when_accept_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "accept failed"):
            await relay_server.relay_connect(AcceptFailWebSocket())  # type: ignore[arg-type]

        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)

    async def test_receive_json_times_out_waiting_for_hello(self) -> None:
        with self.assertRaises(TimeoutError):
            await relay_server._receive_json(WaitingWebSocket(), timeout_seconds=0.01)  # type: ignore[arg-type]

    async def test_session_receive_uses_relay_heartbeat_timeout(self) -> None:
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=WaitingWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )

        with patch("backend.app.relay_server.storage.RELAY_HEARTBEAT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                await relay_server._receive_session_message(session)

    async def test_receive_json_rejects_oversized_message(self) -> None:
        with patch("backend.app.relay_server.RELAY_MAX_FRAME_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "too large"):
                await relay_server._receive_json(TextWebSocket('{"type":"hello"}'))  # type: ignore[arg-type]

    async def test_auth_failure_backoff_increases_and_is_cleared(self) -> None:
        with patch("backend.app.relay_server.RELAY_AUTH_BACKOFF_BASE_SECONDS", 0.25), patch(
            "backend.app.relay_server.RELAY_AUTH_BACKOFF_MAX_SECONDS", 1.0
        ), patch("backend.app.relay_server.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await relay_server._record_auth_failure("edge-1")
            await relay_server._record_auth_failure("edge-1")
            relay_server._clear_auth_failure("edge-1")

        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.25, 0.5])
        self.assertNotIn("edge-1", relay_server.AUTH_FAILURES)

    async def test_connect_rejects_invalid_token_version_without_auth_lookup(self) -> None:
        websocket = ConnectWebSocket(
            '{"type":"hello","runner_id":"edge-1","relay_token":"pgrl_secret","relay_token_version":"abc"}'
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token") as verify_token:
            await relay_server.relay_connect(websocket)  # type: ignore[arg-type]

        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.close_kwargs, {"code": 4400, "reason": "invalid hello"})
        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)
        verify_token.assert_not_called()

    async def test_connect_rejects_unknown_session_message(self) -> None:
        websocket = SessionWebSocket(
            [
                '{"type":"hello","runner_id":"edge-1","relay_token":"pgrl_secret","relay_token_version":1}',
                '{"type":"bogus"}',
            ]
        )

        with patch(
            "backend.app.relay_server.storage.verify_probe_runner_relay_token",
            return_value={"allocated_internal_port": 18001},
        ), patch("backend.app.relay_server._start_internal_server", new=AsyncMock(return_value=FakeServer())), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_connected"
        ), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_disconnected"
        ) as mark_disconnected:
            await relay_server.relay_connect(websocket)  # type: ignore[arg-type]

        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.messages, ['{"type":"ready"}'])
        self.assertEqual(websocket.close_kwargs, {"code": 4400, "reason": "invalid relay message"})
        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)
        self.assertNotIn("edge-1", relay_server.ACTIVE_SESSIONS)
        mark_disconnected.assert_called_once_with("edge-1", "invalid relay message")

    async def test_connect_preserves_disconnect_reason_when_close_fails(self) -> None:
        websocket = FailingCloseSessionWebSocket(
            [
                '{"type":"hello","runner_id":"edge-1","relay_token":"pgrl_secret","relay_token_version":1}',
                '{"type":"bogus"}',
            ]
        )

        with patch(
            "backend.app.relay_server.storage.verify_probe_runner_relay_token",
            return_value={"allocated_internal_port": 18001},
        ), patch("backend.app.relay_server._start_internal_server", new=AsyncMock(return_value=FakeServer())), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_connected"
        ), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_disconnected"
        ) as mark_disconnected:
            await relay_server.relay_connect(websocket)  # type: ignore[arg-type]

        self.assertEqual(websocket.close_kwargs, {"code": 4400, "reason": "invalid relay message"})
        mark_disconnected.assert_called_once_with("edge-1", "invalid relay message")

    async def test_connect_records_unexpected_error_without_default_reason_overwrite(self) -> None:
        websocket = SessionWebSocket(
            [
                '{"type":"hello","runner_id":"edge-1","relay_token":"pgrl_secret","relay_token_version":1}',
            ]
        )

        with patch(
            "backend.app.relay_server.storage.verify_probe_runner_relay_token",
            return_value={"allocated_internal_port": 18001},
        ), patch("backend.app.relay_server._start_internal_server", new=AsyncMock(return_value=FakeServer())), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_connected"
        ), patch.object(
            relay_server.RelaySession,
            "send_json",
            new=AsyncMock(side_effect=RuntimeError("ready send failed")),
        ), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_disconnected"
        ) as mark_disconnected:
            with self.assertRaisesRegex(RuntimeError, "ready send failed"):
                await relay_server.relay_connect(websocket)  # type: ignore[arg-type]

        mark_disconnected.assert_called_once_with("edge-1", "ready send failed")

    async def test_connect_rejects_close_without_stream_id(self) -> None:
        websocket = SessionWebSocket(
            [
                '{"type":"hello","runner_id":"edge-1","relay_token":"pgrl_secret","relay_token_version":1}',
                '{"type":"close"}',
            ]
        )

        with patch(
            "backend.app.relay_server.storage.verify_probe_runner_relay_token",
            return_value={"allocated_internal_port": 18001},
        ), patch("backend.app.relay_server._start_internal_server", new=AsyncMock(return_value=FakeServer())), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_connected"
        ), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_disconnected"
        ) as mark_disconnected:
            await relay_server.relay_connect(websocket)  # type: ignore[arg-type]

        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.close_kwargs, {"code": 4400, "reason": "invalid relay message"})
        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)
        self.assertNotIn("edge-1", relay_server.ACTIVE_SESSIONS)
        mark_disconnected.assert_called_once_with("edge-1", "invalid relay message")


class RelayServerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        relay_server.ACTIVE_SESSIONS.clear()
        relay_server.PUBLIC_CONNECTIONS = 0

    async def _listening_session(self) -> tuple[relay_server.RelaySession, int]:
        server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        websocket = SessionWebSocket([
            '{"type":"hello","runner_id":"edge-1","relay_token":"relay-token","relay_token_version":1}',
            '{"type":"bogus"}',
        ])
        session = relay_server.RelaySession("edge-1", "relay-token", 1, websocket, server)  # type: ignore[arg-type]
        session.streams["stream-1"] = FakeStream()  # type: ignore[assignment]
        self.addAsyncCleanup(session.close)
        return session, port

    async def _assert_listener_released(self, session: relay_server.RelaySession, port: int) -> None:
        self.assertFalse(session.server.is_serving())
        self.assertEqual(session.streams, {})
        self.assertIsNotNone(session.websocket.close_kwargs)
        server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", port)
        server.close()
        await server.wait_closed()

    async def test_disconnect_storage_failure_releases_listener_and_connection_count(self) -> None:
        session, port = await self._listening_session()
        stream = session.streams["stream-1"]
        relay_server.ACTIVE_SESSIONS["edge-1"] = session
        with patch.object(relay_server.storage, "verify_probe_runner_relay_token", return_value={"allocated_internal_port": port}), patch.object(
            relay_server, "_open_relay_session", new=AsyncMock(return_value=session)
        ), patch.object(relay_server.storage, "mark_probe_runner_relay_connected"), patch.object(
            relay_server.storage, "mark_probe_runner_relay_disconnected", side_effect=sqlite3.OperationalError("database is locked")
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                await relay_server.relay_connect(session.websocket)

        self.assertNotIn("edge-1", relay_server.ACTIVE_SESSIONS)
        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)
        self.assertTrue(stream.closed)
        await self._assert_listener_released(session, port)

    async def test_disconnect_close_failure_still_releases_connection_count(self) -> None:
        session, port = await self._listening_session()
        relay_server.ACTIVE_SESSIONS["edge-1"] = session
        with patch.object(relay_server.storage, "verify_probe_runner_relay_token", return_value={"allocated_internal_port": port}), patch.object(
            relay_server, "_open_relay_session", new=AsyncMock(return_value=session)
        ), patch.object(relay_server.storage, "mark_probe_runner_relay_connected"), patch.object(
            relay_server.storage, "mark_probe_runner_relay_disconnected"
        ), patch.object(session, "close", new=AsyncMock(side_effect=RuntimeError("cleanup failed"))):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await relay_server.relay_connect(session.websocket)

        self.assertNotIn("edge-1", relay_server.ACTIVE_SESSIONS)
        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)

    async def test_disconnect_cancellation_still_releases_listener_and_connection_count(self) -> None:
        session, port = await self._listening_session()
        relay_server.ACTIVE_SESSIONS["edge-1"] = session
        with patch.object(relay_server.storage, "verify_probe_runner_relay_token", return_value={"allocated_internal_port": port}), patch.object(
            relay_server, "_open_relay_session", new=AsyncMock(return_value=session)
        ), patch.object(relay_server.storage, "mark_probe_runner_relay_connected"), patch.object(
            relay_server.storage, "mark_probe_runner_relay_disconnected", side_effect=asyncio.CancelledError
        ):
            with self.assertRaises(asyncio.CancelledError):
                await relay_server.relay_connect(session.websocket)

        self.assertEqual(relay_server.PUBLIC_CONNECTIONS, 0)
        await self._assert_listener_released(session, port)

    async def test_revoke_storage_failure_releases_listener(self) -> None:
        session, port = await self._listening_session()
        relay_server.ACTIVE_SESSIONS["edge-1"] = session
        with patch.object(relay_server.storage, "mark_probe_runner_relay_disconnected", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                await relay_server.revoke_relay_session("edge-1")

        self.assertNotIn("edge-1", relay_server.ACTIVE_SESSIONS)
        await self._assert_listener_released(session, port)

    async def test_replacement_storage_failure_releases_port_for_next_connection(self) -> None:
        session, port = await self._listening_session()
        relay_server.ACTIVE_SESSIONS["edge-1"] = session
        with patch.object(relay_server, "RELAY_INTERNAL_LISTEN_HOST", "127.0.0.1"), patch.object(
            relay_server.storage, "mark_probe_runner_relay_disconnected", side_effect=sqlite3.OperationalError("database is locked")
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                await relay_server._open_relay_session("edge-1", "relay-token", 1, FakeWebSocket(), port, lambda _reader, writer: writer.close())  # type: ignore[arg-type]

            self.assertNotIn("edge-1", relay_server.ACTIVE_SESSIONS)
            await self._assert_listener_released(session, port)
            new_session = await relay_server._open_relay_session("edge-1", "relay-token", 1, FakeWebSocket(), port, lambda _reader, writer: writer.close())  # type: ignore[arg-type]
            self.assertIsNotNone(new_session)
            self.addAsyncCleanup(new_session.close)
            self.assertIs(relay_server.ACTIVE_SESSIONS["edge-1"], new_session)
            self.assertTrue(new_session.server.is_serving())


class RelayServerStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        relay_server.ACTIVE_SESSIONS.clear()

    async def test_session_close_continues_after_stream_close_failure(self) -> None:
        websocket = FakeWebSocket()
        failed_stream = FakeStream(fail=True)
        next_stream = FakeStream()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams = {"failed": failed_stream, "next": next_stream}  # type: ignore[assignment]

        await session.close()

        self.assertTrue(failed_stream.closed)
        self.assertTrue(next_stream.closed)
        self.assertEqual(session.streams, {})
        self.assertEqual(websocket.close_kwargs, {})

    async def test_close_stream_removes_stream_after_close_failure(self) -> None:
        failed_stream = FakeStream(fail=True)
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["stream-1"] = failed_stream  # type: ignore[assignment]

        await relay_server._close_stream_best_effort(session, "stream-1")

        self.assertTrue(failed_stream.closed)
        self.assertEqual(session.streams, {})

    async def test_duplicate_session_replaces_old_session(self) -> None:
        old_server = FakeServer()
        old_websocket = FakeWebSocket()
        old_session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=old_websocket,  # type: ignore[arg-type]
            server=old_server,  # type: ignore[arg-type]
        )
        new_websocket = FakeWebSocket()
        relay_server.ACTIVE_SESSIONS["edge-1"] = old_session

        async def handler(_reader: object, _writer: object) -> None:
            return None

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_disconnected"
        ) as mark_disconnected, patch(
            "backend.app.relay_server._start_internal_server",
            new=AsyncMock(return_value=FakeServer()),
        ) as start_server:
            result = await relay_server._open_relay_session(  # type: ignore[arg-type]
                "edge-1",
                "relay-token",
                1,
                new_websocket,
                18001,
                handler,
            )

        self.assertIsNotNone(result)
        self.assertIs(relay_server.ACTIVE_SESSIONS["edge-1"], result)
        self.assertTrue(old_server.closed)
        self.assertIsNone(new_websocket.close_kwargs)
        mark_disconnected.assert_called_once_with("edge-1", "relay session replaced")
        start_server.assert_awaited_once()

    async def test_duplicate_stale_session_is_replaced(self) -> None:
        old_server = FakeServer()
        old_session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=old_server,  # type: ignore[arg-type]
        )
        old_session.last_seen_monotonic -= relay_server.storage.RELAY_HEARTBEAT_TIMEOUT_SECONDS + 1
        new_server = FakeServer()
        new_websocket = FakeWebSocket()
        relay_server.ACTIVE_SESSIONS["edge-1"] = old_session

        async def handler(_reader: object, _writer: object) -> None:
            return None

        with patch("backend.app.relay_server.storage.mark_probe_runner_relay_disconnected") as mark_disconnected, patch(
            "backend.app.relay_server._start_internal_server",
            new=AsyncMock(return_value=new_server),
        ):
            result = await relay_server._open_relay_session(  # type: ignore[arg-type]
                "edge-1",
                "relay-token",
                1,
                new_websocket,
                18001,
                handler,
            )

        self.assertIsNotNone(result)
        self.assertIs(relay_server.ACTIVE_SESSIONS["edge-1"], result)
        self.assertTrue(old_server.closed)
        self.assertIs(result.server, new_server)
        mark_disconnected.assert_called_once_with("edge-1", "relay session replaced")

    async def test_data_frame_revalidates_relay_token_before_writing_stream(self) -> None:
        websocket = FakeWebSocket()
        writer = FakeWriter()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["stream-1"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=EmptyReader(),
            writer=writer,
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value=None):
            keep_running = await relay_server._handle_data_message(
                session,
                {"type": "data", "stream_id": "stream-1", "data": "c2hvdWxkLW5vdC13cml0ZQ=="},
            )

        self.assertFalse(keep_running)
        self.assertEqual(websocket.close_kwargs, {"code": 4403, "reason": "relay token rotated"})
        self.assertEqual(writer.writes, [])

    async def test_data_frame_token_rotation_close_failure_does_not_bubble(self) -> None:
        websocket = FailingCloseWebSocket()
        writer = FakeWriter()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["stream-1"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=EmptyReader(),
            writer=writer,
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value=None):
            keep_running = await relay_server._handle_data_message(
                session,
                {"type": "data", "stream_id": "stream-1", "data": "c2hvdWxkLW5vdC13cml0ZQ=="},
            )

        self.assertFalse(keep_running)
        self.assertEqual(websocket.close_kwargs, {"code": 4403, "reason": "relay token rotated"})
        self.assertEqual(writer.writes, [])

    async def test_tcp_client_token_rotation_sets_session_disconnect_reason(self) -> None:
        websocket = FailingCloseWebSocket()
        writer = FailingCloseWriter()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value=None):
            await relay_server._tcp_client_connected(  # type: ignore[arg-type]
                session,
                EmptyReader(),
                writer,
            )

        self.assertEqual(session.disconnect_reason, "relay token rotated")
        self.assertTrue(writer.closed)
        self.assertEqual(websocket.close_kwargs, {"code": 4403, "reason": "relay token rotated"})

    async def test_tcp_client_send_failure_removes_stream_without_bubbling(self) -> None:
        writer = FakeWriter()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch.object(
            session,
            "send_json",
            new=AsyncMock(side_effect=RuntimeError("relay send failed")),
        ):
            await relay_server._tcp_client_connected(  # type: ignore[arg-type]
                session,
                EmptyReader(),
                writer,
            )

        self.assertTrue(writer.closed)
        self.assertEqual(session.streams, {})

    async def test_data_frame_keeps_session_when_credential_lookup_fails(self) -> None:
        writer = FakeWriter()
        websocket = FakeWebSocket()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["stream-1"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=EmptyReader(),
            writer=writer,
        )

        with patch(
            "backend.app.relay_server.storage.verify_probe_runner_relay_token",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            keep_running = await relay_server._handle_data_message(
                session,
                {"type": "data", "stream_id": "stream-1", "data": "cGF5bG9hZA=="},
            )

        self.assertTrue(keep_running)
        self.assertIsNone(websocket.close_kwargs)
        self.assertEqual(writer.writes, [b"payload"])

    async def test_new_stream_keeps_session_when_credential_lookup_fails(self) -> None:
        writer = FakeWriter()
        websocket = FakeWebSocket()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )

        with patch(
            "backend.app.relay_server.storage.verify_probe_runner_relay_token",
            side_effect=sqlite3.OperationalError("database is locked"),
        ), patch("backend.app.relay_server.storage.get_settings", return_value={"max_concurrency": 1}):
            await relay_server._tcp_client_connected(  # type: ignore[arg-type]
                session,
                EmptyReader(),
                writer,
            )

        self.assertIsNone(websocket.close_kwargs)
        self.assertIsNone(session.disconnect_reason)
        self.assertIn('"type":"open"', websocket.messages[0])

    async def test_data_frame_rejects_invalid_payload_even_for_unknown_stream(self) -> None:
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch(
            "backend.app.relay_server.storage.get_settings",
            return_value={"max_concurrency": 1},
        ):
            with self.assertRaisesRegex(ValueError, "not valid base64"):
                await relay_server._handle_data_message(
                    session,
                    {"type": "data", "stream_id": "missing-stream", "data": "not-base64!"},
                )

    async def test_data_frame_requires_stream_id(self) -> None:
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch(
            "backend.app.relay_server.storage.get_settings",
            return_value={"max_concurrency": 1},
        ):
            with self.assertRaisesRegex(ValueError, "missing stream_id"):
                await relay_server._handle_data_message(
                    session,
                    {"type": "data", "data": "cGF5bG9hZA=="},
                )

    async def test_data_frame_drain_timeout_removes_closed_stream(self) -> None:
        writer = SlowDrainWriter()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["stream-1"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=EmptyReader(),
            writer=writer,
        )

        with patch("backend.app.relay_server.RELAY_STREAM_IDLE_TIMEOUT_SECONDS", 0.01), patch(
            "backend.app.relay_server.storage.verify_probe_runner_relay_token",
            return_value={"ok": True},
        ):
            keep_running = await relay_server._handle_data_message(
                session,
                {"type": "data", "stream_id": "stream-1", "data": "cGF5bG9hZA=="},
            )

        self.assertTrue(keep_running)
        self.assertEqual(writer.writes, [b"payload"])
        self.assertTrue(writer.closed)
        self.assertNotIn("stream-1", session.streams)

    async def test_data_frame_write_failure_removes_closed_stream(self) -> None:
        writer = FailingWriteWriter()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["stream-1"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=EmptyReader(),
            writer=writer,
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}):
            keep_running = await relay_server._handle_data_message(
                session,
                {"type": "data", "stream_id": "stream-1", "data": "cGF5bG9hZA=="},
            )

        self.assertTrue(keep_running)
        self.assertEqual(writer.writes, [b"payload"])
        self.assertTrue(writer.closed)
        self.assertNotIn("stream-1", session.streams)

    async def test_internal_server_binds_configured_internal_host(self) -> None:
        async def handler(_reader: object, _writer: object) -> None:
            return None

        server = object()
        with patch("backend.app.relay_server.RELAY_INTERNAL_LISTEN_HOST", "pulseguard-relay-internal"), patch(
            "backend.app.relay_server.asyncio.start_server",
            new=AsyncMock(return_value=server),
        ) as start_server:
            result = await relay_server._start_internal_server(handler, 18001)

        self.assertIs(result, server)
        start_server.assert_awaited_once_with(handler, host="pulseguard-relay-internal", port=18001)

    async def test_tcp_client_connection_rejects_extra_streams(self) -> None:
        websocket = FakeWebSocket()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["existing"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=BlockingReader(),
            writer=FakeWriter(),
        )
        second_writer = FakeWriter()

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch(
            "backend.app.relay_server._max_concurrent_streams",
            return_value=1,
        ):
            await relay_server._tcp_client_connected(  # type: ignore[arg-type]
                session,
                EmptyReader(),
                second_writer,
            )

        self.assertTrue(second_writer.closed)
        self.assertEqual(sum('"type":"open"' in message for message in websocket.messages), 0)

    async def test_tcp_client_connection_rejects_extra_stream_close_failure(self) -> None:
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=FakeWebSocket(),  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        session.streams["existing"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=EmptyReader(),
            writer=FakeWriter(),
        )
        rejected_writer = FailingCloseWriter()

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch(
            "backend.app.relay_server._max_concurrent_streams",
            return_value=1,
        ):
            await relay_server._tcp_client_connected(  # type: ignore[arg-type]
                session,
                EmptyReader(),
                rejected_writer,
            )

        self.assertTrue(rejected_writer.closed)
        self.assertIn("existing", session.streams)

    async def test_tcp_client_connection_limit_uses_execution_concurrency_setting(self) -> None:
        websocket = FakeWebSocket()
        session = relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=FakeServer(),  # type: ignore[arg-type]
        )
        first_reader = BlockingReader()
        second_reader = BlockingReader()
        first_writer = FakeWriter()
        second_writer = FakeWriter()

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch(
            "backend.app.relay_server.storage.get_settings",
            return_value={"max_concurrency": 2},
        ):
            first = asyncio.create_task(relay_server._tcp_client_connected(session, first_reader, first_writer))  # type: ignore[arg-type]
            second = asyncio.create_task(relay_server._tcp_client_connected(session, second_reader, second_writer))  # type: ignore[arg-type]
            for _ in range(50):
                if len(session.streams) == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(session.streams), 2)
            first_reader.done.set()
            second_reader.done.set()
            await asyncio.gather(first, second)

        self.assertEqual(sum('"type":"open"' in message for message in websocket.messages), 2)
        self.assertTrue(first_writer.closed)
        self.assertTrue(second_writer.closed)

    async def test_busy_execution_stream_still_allows_health_connection(self) -> None:
        websocket = FakeWebSocket()
        session = relay_server.RelaySession("edge-1", "secret", 1, websocket, FakeServer())
        session.streams["busy-run"] = relay_server.TunnelStream(BlockingReader(), FakeWriter())
        writer = FakeWriter()
        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch(
            "backend.app.relay_server.storage.get_settings", return_value={"max_concurrency": 1}
        ):
            await relay_server._tcp_client_connected(session, EmptyReader(), writer)
        self.assertEqual(sum('"type":"open"' in message for message in websocket.messages), 1)
        self.assertIn("busy-run", session.streams)
        await session.close()

    async def test_stream_limit_remains_bounded_with_auxiliary_headroom(self) -> None:
        with patch("backend.app.relay_server.storage.get_settings", return_value={"max_concurrency": 5}), patch(
            "backend.app.relay_server.RELAY_MAX_CONCURRENT_STREAMS", 2
        ):
            self.assertEqual(relay_server._max_concurrent_streams(), 9)
        with patch("backend.app.relay_server.storage.get_settings", side_effect=RuntimeError), patch(
            "backend.app.relay_server.RELAY_MAX_CONCURRENT_STREAMS", 2
        ):
            self.assertEqual(relay_server._max_concurrent_streams(), 6)

    async def test_disconnect_journal_keeps_session_and_stream_reason_without_peer_secrets(self) -> None:
        from fastapi import WebSocketDisconnect
        class DisconnectSocket(SessionWebSocket):
            async def receive_text(self):
                if not self.texts:
                    raise WebSocketDisconnect(code=1006, reason="secret-in-close")
                return await super().receive_text()
        websocket = DisconnectSocket([
            '{"type":"hello","runner_id":"edge-1","relay_token":"secret-token","relay_token_version":1}',
            '{"type":"error","stream_id":"s1","reason":"worker_connect_failed","message":"secret-in-error"}',
            '{"type":"error","stream_id":"s2","error_type":{"secret":"do-not-log"}}',
        ])
        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"allocated_internal_port": 18001}), patch(
            "backend.app.relay_server._start_internal_server", new=AsyncMock(return_value=FakeServer())
        ), patch("backend.app.relay_server.storage.mark_probe_runner_relay_connected"), patch(
            "backend.app.relay_server.storage.mark_probe_runner_relay_disconnected"
        ) as mark_disconnected, self.assertLogs("uvicorn.error.relay", level="INFO") as logs:
            await relay_server.relay_connect(websocket)
        text = "\n".join(logs.output)
        for expected in ("relay_connected", "relay_disconnected", "session_id=", "edge-1", "worker_connect_failed", "1006"):
            self.assertIn(expected, text)
        self.assertNotIn("secret-", text)
        self.assertNotIn("do-not-log", text)
        mark_disconnected.assert_called_once_with("edge-1", "websocket disconnected (code=1006)")


class RelayFailureAttributionTests(unittest.IsolatedAsyncioTestCase):
    """A tunnel teardown must say why it stopped, not just that something raised."""

    @staticmethod
    def _session(websocket: object, server: object) -> relay_server.RelaySession:
        return relay_server.RelaySession(
            runner_id="edge-1",
            token="relay-token",
            token_version=1,
            websocket=websocket,  # type: ignore[arg-type]
            server=server,  # type: ignore[arg-type]
        )

    async def test_send_json_names_the_closing_state_instead_of_a_socket_error(self) -> None:
        websocket = FakeWebSocket()
        session = self._session(websocket, FakeServer())
        session.closing = True

        with self.assertRaises(RuntimeError) as failure:
            await session.send_json({"type": "open", "stream_id": "stream-1"})

        self.assertEqual(str(failure.exception), "relay session is closing")
        self.assertEqual(websocket.messages, [])

    async def test_an_incoming_tcp_connection_is_dropped_while_the_session_closes(self) -> None:
        websocket = FakeWebSocket()
        writer = FakeWriter()
        session = self._session(websocket, FakeServer())
        session.closing = True

        with patch.object(session, "send_json", new=AsyncMock()) as send_json:
            await relay_server._tcp_client_connected(session, EmptyReader(), writer)  # type: ignore[arg-type]

        send_json.assert_not_awaited()
        self.assertTrue(writer.closed)
        self.assertEqual(session.streams, {})

    async def test_a_failed_open_records_the_reason_the_stream_could_not_start(self) -> None:
        session = self._session(FakeWebSocket(), FakeServer())
        writer = FakeWriter()

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), patch.object(
            session, "send_json", new=AsyncMock(side_effect=ConnectionResetError("peer closed without shutdown"))
        ), self.assertLogs("uvicorn.error.relay", level="WARNING") as logs:
            await relay_server._tcp_client_connected(session, EmptyReader(), writer)  # type: ignore[arg-type]

        text = "\n".join(logs.output)
        self.assertIn("relay_stream_failed", text)
        self.assertIn("ConnectionResetError", text)
        self.assertIn("peer closed without shutdown", text)

    async def test_a_failed_stream_write_records_the_reason(self) -> None:
        session = self._session(FakeWebSocket(), FakeServer())
        session.streams["stream-1"] = relay_server.TunnelStream(  # type: ignore[arg-type]
            reader=EmptyReader(), writer=FailingWriteWriter()
        )

        with patch("backend.app.relay_server.storage.verify_probe_runner_relay_token", return_value={"ok": True}), self.assertLogs(
            "uvicorn.error.relay", level="WARNING"
        ) as logs:
            await relay_server._handle_data_message(
                session,
                {"type": "data", "stream_id": "stream-1", "data": "cGF5bG9hZA=="},
            )

        text = "\n".join(logs.output)
        self.assertIn("relay_stream_failed", text)
        self.assertIn("stream write failed", text)
