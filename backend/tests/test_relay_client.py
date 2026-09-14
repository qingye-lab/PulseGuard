from __future__ import annotations

import asyncio
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from backend.app import relay_client
from backend.app.relay_tunnel import TunnelStream


class FakeReader:
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


class FailingWriteWriter(FakeWriter):
    def write(self, data: bytes) -> None:
        super().write(data)
        raise RuntimeError("worker stream write failed")


class FailingCloseWriter(FakeWriter):
    async def wait_closed(self) -> None:
        raise RuntimeError("worker stream close failed")


class FakeSslObject:
    def __init__(self, cert: bytes | None) -> None:
        self.cert = cert

    def getpeercert(self, *, binary_form: bool = False) -> bytes | None:
        return self.cert if binary_form else None


class FakeTransport:
    def __init__(self, cert: bytes | None) -> None:
        self.ssl_object = FakeSslObject(cert) if cert is not None else None

    def get_extra_info(self, name: str) -> object | None:
        return self.ssl_object if name == "ssl_object" else None


class FakeWebsocket:
    def __init__(self, cert: bytes | None) -> None:
        self.transport = FakeTransport(cert)


class FailingSendWebsocket:
    async def send(self, _message: str) -> None:
        raise RuntimeError("websocket send failed")


class RelayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_connect_timeout_reports_stream_failure_without_hanging(self) -> None:
        async def blocked(*args):
            await asyncio.Event().wait()
        with patch("backend.app.relay_client.asyncio.open_connection", side_effect=blocked), patch(
            "backend.app.relay_client.WORKER_CONNECT_TIMEOUT_SECONDS", 0.01
        ), patch("backend.app.relay_client._send_json_best_effort", new_callable=AsyncMock) as send:
            await asyncio.wait_for(relay_client._open_worker_stream("s1", object(), asyncio.Lock(), {}), 1)
        self.assertEqual(send.call_args.args[2]["reason"], "worker_connect_failed")

    async def test_clean_and_failed_disconnects_both_back_off(self) -> None:
        for outcomes in ([None, None, asyncio.CancelledError()], [ConnectionError(), ConnectionError(), asyncio.CancelledError()]):
            with self.subTest(outcomes=outcomes), patch("backend.app.relay_client.run_once", new=AsyncMock(side_effect=outcomes)), patch(
                "backend.app.relay_client.asyncio.sleep", new_callable=AsyncMock
            ) as sleep, patch("backend.app.relay_client.random.uniform", side_effect=lambda low, high: high):
                with self.assertRaises(asyncio.CancelledError):
                    await relay_client.main_loop()
                self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])

    async def test_stable_session_resets_reconnect_backoff(self) -> None:
        with patch("backend.app.relay_client.run_once", new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()])), patch(
            "backend.app.relay_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep, patch("backend.app.relay_client.random.uniform", side_effect=lambda low, high: high), patch(
            "backend.app.relay_client.time.monotonic", side_effect=[0, 0, 0, 65, 65]
        ):
            with self.assertRaises(asyncio.CancelledError):
                await relay_client.main_loop()
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 1])

    async def test_fingerprint_requires_peer_certificate(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "TLS certificate is unavailable"):
            relay_client._assert_fingerprint(FakeWebsocket(None), "00")  # type: ignore[arg-type]

    async def test_fingerprint_accepts_matching_certificate(self) -> None:
        cert = b"relay-cert"
        relay_client._assert_fingerprint(  # type: ignore[arg-type]
            FakeWebsocket(cert),
            hashlib.sha256(cert).hexdigest(),
        )

    async def test_relay_url_must_use_wss_before_sending_token(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PULSEGUARD_RELAY_URL": "ws://127.0.0.1:9443/relay/connect",
                "PULSEGUARD_RUNNER_ID": "edge-1",
                "PULSEGUARD_RELAY_TOKEN": "pgrl_secret",
                "PULSEGUARD_RELAY_FINGERPRINT": "00",
            },
            clear=False,
        ), patch("backend.app.relay_client.websockets.connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "must use wss://"):
                await relay_client.run_once()

        connect.assert_not_called()

    async def test_relay_token_version_must_be_integer_before_connecting(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "PULSEGUARD_RELAY_URL": "wss://127.0.0.1:9443/relay/connect",
                "PULSEGUARD_RUNNER_ID": "edge-1",
                "PULSEGUARD_RELAY_TOKEN": "pgrl_secret",
                "PULSEGUARD_RELAY_FINGERPRINT": "00",
                "PULSEGUARD_RELAY_TOKEN_VERSION": "bad",
            },
            clear=False,
        ), patch("backend.app.relay_client.websockets.connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                await relay_client.run_once()

        connect.assert_not_called()

    async def test_worker_endpoint_rejects_invalid_port(self) -> None:
        with patch.dict("os.environ", {"PULSEGUARD_WORKER_URL": "http://pulseguard-worker:bad"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "port is invalid"):
                relay_client._worker_endpoint()

    async def test_worker_pump_uses_idle_timeout_and_removes_closed_stream(self) -> None:
        writer = FakeWriter()
        streams: dict[str, TunnelStream] = {
            "stream-1": TunnelStream(reader=FakeReader(), writer=writer)  # type: ignore[arg-type]
        }

        with patch("backend.app.relay_client.RELAY_STREAM_IDLE_TIMEOUT_SECONDS", 12), patch(
            "backend.app.relay_client.pump_reader_to_sender",
            new_callable=AsyncMock,
        ) as pump_reader_to_sender:
            await relay_client._pump_worker_to_relay(  # type: ignore[arg-type]
                "stream-1",
                FakeReader(),
                object(),
                asyncio.Lock(),
                streams,
            )

        pump_reader_to_sender.assert_awaited_once()
        self.assertEqual(pump_reader_to_sender.call_args.kwargs["idle_timeout_seconds"], 12)
        self.assertTrue(writer.closed)
        self.assertNotIn("stream-1", streams)

    async def test_worker_pump_failure_removes_stream_without_bubbling(self) -> None:
        writer = FakeWriter()
        streams: dict[str, TunnelStream] = {
            "stream-1": TunnelStream(reader=FakeReader(), writer=writer)  # type: ignore[arg-type]
        }

        with patch(
            "backend.app.relay_client.pump_reader_to_sender",
            new_callable=AsyncMock,
            side_effect=RuntimeError("relay send failed"),
        ):
            await relay_client._pump_worker_to_relay(  # type: ignore[arg-type]
                "stream-1",
                FakeReader(),
                object(),
                asyncio.Lock(),
                streams,
            )

        self.assertTrue(writer.closed)
        self.assertNotIn("stream-1", streams)

    async def test_duplicate_open_frame_fails_before_worker_connection(self) -> None:
        writer = FakeWriter()
        streams: dict[str, TunnelStream] = {
            "stream-1": TunnelStream(reader=FakeReader(), writer=writer)  # type: ignore[arg-type]
        }

        with patch("backend.app.relay_client.asyncio.open_connection", new_callable=AsyncMock) as open_connection:
            with self.assertRaisesRegex(RuntimeError, "invalid relay message"):
                await relay_client._handle_relay_message(
                    {"type": "open", "stream_id": "stream-1"},
                    object(),
                    asyncio.Lock(),
                    streams,
                )

        open_connection.assert_not_called()
        self.assertIs(streams["stream-1"].writer, writer)

    async def test_worker_open_error_report_is_best_effort(self) -> None:
        streams: dict[str, TunnelStream] = {}

        with patch("backend.app.relay_client._worker_endpoint", return_value=("pulseguard-worker", 8788)), patch(
            "backend.app.relay_client.asyncio.open_connection",
            new=AsyncMock(side_effect=OSError("connection refused")),
        ):
            await relay_client._open_worker_stream("stream-1", FailingSendWebsocket(), asyncio.Lock(), streams)

        self.assertEqual(streams, {})

    async def test_close_frame_removes_stream_after_close_failure(self) -> None:
        writer = FailingCloseWriter()
        streams: dict[str, TunnelStream] = {
            "stream-1": TunnelStream(reader=FakeReader(), writer=writer)  # type: ignore[arg-type]
        }

        await relay_client._handle_relay_message(
            {"type": "close", "stream_id": "stream-1"},
            object(),
            asyncio.Lock(),
            streams,
        )

        self.assertTrue(writer.closed)
        self.assertEqual(streams, {})

    async def test_relay_data_closes_worker_stream_when_drain_times_out(self) -> None:
        writer = SlowDrainWriter()
        streams: dict[str, TunnelStream] = {
            "stream-1": TunnelStream(reader=FakeReader(), writer=writer)  # type: ignore[arg-type]
        }

        with patch("backend.app.relay_client.RELAY_STREAM_IDLE_TIMEOUT_SECONDS", 0.01):
            await relay_client._handle_relay_data_message(
                "stream-1",
                {"type": "data", "stream_id": "stream-1", "data": "cGF5bG9hZA=="},
                streams,
            )

        self.assertEqual(writer.writes, [b"payload"])
        self.assertTrue(writer.closed)
        self.assertNotIn("stream-1", streams)

    async def test_relay_data_closes_worker_stream_when_write_fails(self) -> None:
        writer = FailingWriteWriter()
        streams: dict[str, TunnelStream] = {
            "stream-1": TunnelStream(reader=FakeReader(), writer=writer)  # type: ignore[arg-type]
        }

        await relay_client._handle_relay_data_message(
            "stream-1",
            {"type": "data", "stream_id": "stream-1", "data": "cGF5bG9hZA=="},
            streams,
        )

        self.assertEqual(writer.writes, [b"payload"])
        self.assertTrue(writer.closed)
        self.assertNotIn("stream-1", streams)

    async def test_relay_data_rejects_invalid_payload_even_for_unknown_stream(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid base64"):
            await relay_client._handle_relay_data_message(
                "missing-stream",
                {"type": "data", "stream_id": "missing-stream", "data": "not-base64!"},
                {},
            )

    async def test_heartbeat_ack_is_valid_noop(self) -> None:
        await relay_client._handle_relay_message({"type": "heartbeat_ack"}, object(), asyncio.Lock(), {})

    async def test_unknown_relay_message_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid relay message"):
            await relay_client._handle_relay_message({"type": "bogus"}, object(), asyncio.Lock(), {})
