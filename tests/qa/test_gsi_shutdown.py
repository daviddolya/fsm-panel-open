import asyncio
import contextlib
import json

import pytest

from fsm_panel.gsi import GameStateReceiver, GsiServer


async def test_cancelled_close_cannot_orphan_a_connection_that_still_updates_telemetry():
    receiver = GameStateReceiver()
    token = "synthetic-gsi-cancel-token-" + "x" * 32
    receiver.register("synthetic-account", "76561198000000001", token)
    encoded = json.dumps(
        {
            "provider": {"steamid": "76561198000000001"},
            "auth": {"token": token},
            "round": 17,
        }
    ).encode()
    server = await GsiServer(receiver, body_timeout=3).start()
    # Keep this reference solely to clean up a failed implementation's orphan.
    owned_server = server._server
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        writer.write(
            f"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(encoded)}\r\n\r\n".encode()
            + encoded[:1]
        )
        await writer.drain()
        async with asyncio.timeout(2):
            while not owned_server.server_state.tasks:
                await asyncio.sleep(0)

        closing = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=5)
        # Retrying close must either finish retained cleanup or observe that it
        # has already finished. It must not silently forget the old connection.
        await asyncio.wait_for(server.close(), timeout=5)
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(encoded[1:])
            await writer.drain()
        with contextlib.suppress(ConnectionError, OSError):
            await asyncio.wait_for(reader.read(), timeout=4)
        assert receiver.snapshot("synthetic-account") is None
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        await server.close()
        await owned_server.shutdown()
