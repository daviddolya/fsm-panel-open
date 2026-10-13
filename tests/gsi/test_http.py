import asyncio
import contextlib
import json
import logging
import signal
import socket

import httpx
import pytest

from fsm_panel.gsi import GameStateReceiver, GsiServer, create_gsi_app

STEAM_ID = "76561198000000001"
TOKEN = "synthetic-http-token-" + "a" * 32


@pytest.fixture
def receiver():
    receiver = GameStateReceiver()
    receiver.register("synthetic-account", STEAM_ID, TOKEN)
    return receiver


def body(**extra):
    return {"provider": {"steamid": STEAM_ID}, "auth": {"token": TOKEN}, **extra}


def client(receiver, host="127.0.0.1", **kwargs):
    transport = httpx.ASGITransport(app=create_gsi_app(receiver, **kwargs), client=(host, 12345))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


async def test_real_loopback_server_response_lifecycle_and_redaction(receiver):
    before_signal = signal.getsignal(signal.SIGINT)
    server = GsiServer(receiver)
    with pytest.raises(RuntimeError, match="gsi_server_not_running"):
        _ = server.port
    async with server:
        port = server.port
        assert 0 < port <= 65535
        assert await server.start() is server
        async with httpx.AsyncClient(trust_env=False) as connection:
            response = await connection.post(server.url, json=body(round=7))
            assert response.status_code == 200
            assert response.content == b""
            assert response.headers["content-type"] == "text/html; charset=utf-8"
            assert (await connection.get(server.url)).status_code == 405
            # Forwarding headers never select an identity or override the peer address.
            response = await connection.post(
                server.url, json=body(round=8), headers={"x-forwarded-for": "203.0.113.9"}
            )
            assert response.status_code == 200
    assert signal.getsignal(signal.SIGINT) == before_signal
    assert receiver.snapshot("synthetic-account").payload["round"] == 8
    assert "auth" not in receiver.snapshot("synthetic-account").payload
    await server.close()
    with socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    async with server:
        async with httpx.AsyncClient(trust_env=False) as connection:
            assert (await connection.post(server.url, json=body())).status_code == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "192.0.2.1", "127.0.0.2"])
def test_server_rejects_every_binding_except_literal_ipv4_loopback(receiver, host):
    with pytest.raises(ValueError, match="gsi_bind_must_be_127_0_0_1"):
        GsiServer(receiver, host=host)


async def test_nonlocal_request_cannot_spoof_loopback_with_proxy_header(receiver):
    async with client(receiver, "203.0.113.2") as connection:
        response = await connection.post("/", json=body(), headers={"x-forwarded-for": "127.0.0.1"})
    assert response.status_code == 403
    assert response.json() == {"error": "loopback_required"}
    assert receiver.snapshot("synthetic-account") is None


async def test_http_auth_and_error_responses_never_echo_payload(receiver, caplog):
    marker = "private-payload-marker"
    with caplog.at_level(logging.DEBUG):
        async with client(receiver) as connection:
            wrong_auth = body(private=marker)
            wrong_auth["auth"]["token"] = "incorrect-token-" + "b" * 32
            wrong = await connection.post("/", json=wrong_auth)
            malformed = await connection.post("/", content=f'{{"secret":"{TOKEN}{marker}"')
            missing = await connection.post("/", json={"provider": {"steamid": STEAM_ID}})
    assert wrong.json() == missing.json() == {"error": "unauthorized"}
    assert wrong.status_code == missing.status_code == 401
    assert malformed.json() == {"error": "invalid_payload"}
    assert malformed.status_code == 400
    assert TOKEN not in caplog.text
    assert marker not in caplog.text
    assert receiver.snapshot("synthetic-account") is None


async def test_body_limits_apply_to_streamed_actual_length_and_declared_length(receiver):
    async def chunks():
        yield b"x" * receiver.max_payload_bytes
        yield b"y"

    async with client(receiver) as connection:
        declared = await connection.post(
            "/", content=b"", headers={"content-length": str(receiver.max_payload_bytes + 1)}
        )
        streamed = await connection.post("/", content=chunks())
        mismatch = await connection.post("/", content=b"{}", headers={"content-length": "1"})
        invalid = await connection.post("/", content=b"{}", headers={"content-length": "-1"})
        encoded = await connection.post("/", content=b"{}", headers={"content-encoding": "gzip"})
    assert declared.status_code == streamed.status_code == 413
    assert declared.json() == streamed.json() == {"error": "payload_too_large"}
    assert mismatch.json() == invalid.json() == {"error": "invalid_content_length"}
    assert encoded.status_code == 415
    assert receiver.snapshot("synthetic-account") is None


async def test_slow_incomplete_body_times_out_without_storing(receiver):
    async def chunks():
        yield b"{"
        await asyncio.sleep(1)

    async with client(receiver, body_timeout=0.01) as connection:
        response = await connection.post("/", content=chunks())
    assert response.status_code == 408
    assert response.json() == {"error": "body_timeout"}
    assert receiver.snapshot("synthetic-account") is None


async def test_reception_remains_available_without_any_lease_or_game_adapter(receiver):
    async with client(receiver) as connection:
        # An old remote timestamp has no license or game-action semantics.
        response = await connection.post("/", json=body(timestamp=0, phase="gameover"))
    assert response.status_code == 200
    assert receiver.snapshot("synthetic-account").payload["phase"] == "gameover"


async def test_port_conflict_is_safe_and_server_can_close_after_failed_start(receiver):
    async with GsiServer(receiver) as first:
        second = GsiServer(receiver, port=first.port)
        with pytest.raises(RuntimeError, match="^gsi_bind_failed$"):
            await second.start()
        await second.close()


async def test_chunked_json_is_accepted_over_real_http(receiver):
    encoded = json.dumps(body(round=3)).encode()

    async def chunks():
        yield encoded[:20]
        yield encoded[20:]

    async with GsiServer(receiver) as server:
        async with httpx.AsyncClient(trust_env=False) as connection:
            response = await connection.post(server.url, content=chunks())
    assert response.status_code == 200
    assert receiver.snapshot("synthetic-account").payload["round"] == 3


async def test_close_during_incomplete_request_releases_listener_and_does_not_store(receiver):
    server = await GsiServer(receiver, body_timeout=0.05).start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        writer.write(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000\r\n\r\n{")
        await writer.drain()
        await asyncio.sleep(0.02)
        await server.close()
        response = await asyncio.wait_for(reader.read(), timeout=1)
        assert b"408 Request Timeout" in response
        assert receiver.snapshot("synthetic-account") is None
    finally:
        writer.close()
        await writer.wait_closed()
        await server.close()


async def test_ambiguous_content_length_is_rejected(receiver):
    async with client(receiver) as connection:
        response = await connection.post(
            "/", content=b"{}", headers=[("content-length", "2"), ("content-length", "2")]
        )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_content_length"}


async def test_repeatedly_cancelled_close_joins_owned_connections_before_returning(receiver):
    server = await GsiServer(receiver, body_timeout=3).start()
    port = server.port
    owned_server, owned_serve_task = server._server, server._task
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    encoded = json.dumps(body(round=17)).encode()
    try:
        writer.write(
            f"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(encoded)}\r\n\r\n".encode()
            + encoded[:1]
        )
        await writer.drain()
        async with asyncio.timeout(2):
            while not owned_server.server_state.tasks:
                await asyncio.sleep(0)
        owned_requests = tuple(owned_server.server_state.tasks)
        closing = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        # Keep cancelling throughout graceful shutdown and final cleanup, rather
        # than only sending multiple cancellations at the first await point.
        async with asyncio.timeout(6):
            while not closing.done():
                closing.cancel()
                await asyncio.sleep(0.01)
        with pytest.raises(asyncio.CancelledError):
            await closing
        await asyncio.wait_for(server.close(), timeout=1)

        with contextlib.suppress(ConnectionError, OSError):
            writer.write(encoded[1:])
            await writer.drain()
        with contextlib.suppress(ConnectionError, OSError):
            await asyncio.wait_for(reader.read(), timeout=1)
        assert receiver.snapshot("synthetic-account") is None
        assert owned_serve_task.done()
        assert all(task.done() for task in owned_requests)
        assert not owned_server.server_state.connections
        assert not owned_server.server_state.tasks
        with socket.socket() as probe:
            probe.settimeout(0.5)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        # Shutdown also leaves the same object safe to start again.
        async with server:
            async with httpx.AsyncClient(trust_env=False) as connection:
                assert (await connection.post(server.url, json=body(round=18))).status_code == 200
        assert receiver.snapshot("synthetic-account").payload["round"] == 18
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        await server.close()
        # Keep failed pre-fix runs from leaving a live connection behind.
        await owned_server.shutdown()
