import asyncio
import logging
import traceback
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from fsm_panel.notifications import (
    ConfigurationCode,
    DeliveryState,
    ProxySettings,
    RequestTimeouts,
    TelegramCancelled,
    TelegramClient,
    TelegramConfigurationError,
)

TOKEN = "12345:SYNTHETIC_NOT_A_REAL_TOKEN"
PRIVATE = "synthetic-sensitive-content"


def success(message_id=71):
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


async def test_html_text_and_plain_bot_wrapper_use_forms_and_official_origin():
    requests = []

    def handler(request):
        requests.append(request)
        return success()

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        html = await client.send_text(-101, "<b>A & B</b>", thread_id="007")
        plain = await client.send_text(102, "<b>literal</b>", html=False)
        invalid = await client.send_text(102, "message", html=False, thread_id=7)
        assert html.state == plain.state == DeliveryState.SENT
        assert html.message_id == 71
        assert invalid.state == DeliveryState.NOT_SENT
        assert client.pending_count == 0
    assert client.is_closed
    assert len(requests) == 2
    for request in requests:
        assert request.method == "POST"
        assert request.url.scheme == "https"
        assert request.url.host == "api.telegram.org"
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        assert request.url.query == b""
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 40.0,
            "write": 40.0,
            "pool": 5.0,
        }
    assert parse_qs(requests[0].content.decode()) == {
        "chat_id": ["-101"],
        "text": ["<b>A & B</b>"],
        "parse_mode": ["HTML"],
        "message_thread_id": ["007"],
    }
    assert parse_qs(requests[1].content.decode()) == {
        "chat_id": ["102"],
        "text": ["<b>literal</b>"],
    }


async def test_repeated_path_photo_parts_and_raw_bytes_wrapper(tmp_path):
    first, second = tmp_path / "original.jpg", tmp_path / "other.webp"
    first.write_bytes(b"synthetic-image-A")
    second.write_bytes(b"synthetic-image-B")
    requests = []

    def handler(request):
        requests.append(request)
        return success()

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert (
            await client.send_photo_paths(-10, "raw <b>&</b>", [first, second], thread_id=7)
        ).state == "sent"
        assert (
            await client.send_photo_bytes(
                -10, b"synthetic-bytes", "raw <b>&</b>", reply_to_message_id=55
            )
        ).state == "sent"
    assert len(requests) == 2
    path_body, byte_body = (request.content for request in requests)
    assert path_body.count(b'name="photo"; filename="photo"') == 2
    assert path_body.count(b"Content-Type: image/png") == 2
    assert b"original.jpg" not in path_body and b"other.webp" not in path_body
    assert b'name="message_thread_id"\r\n\r\n7' in path_body
    assert b'name="parse_mode"\r\n\r\nHTML' in path_body
    assert b"synthetic-image-A" in path_body and b"synthetic-image-B" in path_body
    assert b'name="photo"; filename="upload"' in byte_body  # Pinned HTTPX raw-bytes default.
    assert b'name="reply_to_message_id"\r\n\r\n55' in byte_body
    assert b"message_thread_id" not in byte_body and b"parse_mode" not in byte_body
    assert b"raw <b>&</b>" in path_body and b"raw <b>&</b>" in byte_body


async def test_edit_fields_and_receipt_identity():
    seen = []

    def handler(request):
        seen.append(request)
        return success(71 if len(seen) < 3 else 72)

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert (await client.edit_text(1, 71, "<b>edit</b>")).state == "sent"
        assert (await client.edit_caption(1, 71, "")).state == "sent"
        assert (await client.edit_text(1, 71, "edit")).state == "unknown_outcome"
    assert [request.url.path.rsplit("/", 1)[-1] for request in seen] == [
        "editMessageText",
        "editMessageCaption",
        "editMessageText",
    ]
    assert parse_qs(seen[1].content.decode(), keep_blank_values=True) == {
        "chat_id": ["1"],
        "caption": [""],
        "message_id": ["71"],
        "parse_mode": ["HTML"],
    }


@pytest.mark.parametrize(
    "status,body,state,code,error_code",
    [
        (200, b'{"ok":false}', "rejected", "remote_rejected", None),
        (
            429,
            b'{"ok":false,"error_code":429,"description":"synthetic-sensitive-content",'
            b'"parameters":{"retry_after":0}}',
            "rejected",
            "remote_rejected",
            429,
        ),
        (
            400,
            b'{"ok":false,"error_code":"synthetic-sensitive-content"}',
            "rejected",
            "remote_rejected",
            None,
        ),
        (200, b'{"ok":1,"result":{"message_id":1}}', "unknown_outcome", "invalid_response", None),
        (
            200,
            b'{"ok":true,"result":{"message_id":true}}',
            "unknown_outcome",
            "invalid_response",
            None,
        ),
        (
            200,
            b'{"ok":true,"result":{"message_id":0}}',
            "unknown_outcome",
            "invalid_response",
            None,
        ),
        (
            200,
            b'{"ok":true,"result":{"message_id":"1"}}',
            "unknown_outcome",
            "invalid_response",
            None,
        ),
        (200, b'{"ok":true}', "unknown_outcome", "invalid_response", None),
        (200, b"null", "unknown_outcome", "invalid_response", None),
        (200, b"synthetic-sensitive-content", "unknown_outcome", "invalid_response", None),
        (503, b'{"ok":true,"result":{"message_id":1}}', "unknown_outcome", "http_status", None),
        (302, b'{"ok":false}', "unknown_outcome", "http_status", None),
    ],
)
async def test_negative_and_malformed_responses_never_retry_or_fall_back(
    status, body, state, code, error_code
):
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            status, content=body, headers={"Location": "https://example.invalid/"}
        )

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.send_photo_bytes(1, b"photo", "caption")
        assert result.state == state and result.code == code and result.error_code == error_code
        assert result.message_id is None
        assert PRIVATE not in repr(result)
    assert calls == ["sendPhoto"]


@pytest.mark.parametrize(
    "error_type,state,code",
    [
        (httpx.ConnectError, "not_sent", "connect_failed"),
        (httpx.ConnectTimeout, "not_sent", "connect_failed"),
        (httpx.PoolTimeout, "not_sent", "connect_failed"),
        (httpx.ReadTimeout, "unknown_outcome", "timeout"),
        (httpx.WriteTimeout, "unknown_outcome", "timeout"),
        (httpx.ReadError, "unknown_outcome", "transport_error"),
        (httpx.WriteError, "unknown_outcome", "transport_error"),
        (httpx.RemoteProtocolError, "unknown_outcome", "transport_error"),
    ],
)
async def test_transport_failures_are_sanitized_and_never_retried(error_type, state, code):
    calls = []

    def handler(request):
        calls.append(1)
        raise error_type(f"{PRIVATE} {request.url}", request=request)

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.send_photo_bytes(1, b"photo", "caption")
        assert result.state == state and result.code == code
        assert PRIVATE not in repr(result) and TOKEN not in repr(result)
    assert calls == [1]


async def test_wall_deadline_after_remote_side_effect_is_unknown_and_cancels_owned_request():
    effects = []
    handler_stopped = asyncio.Event()

    async def handler(request):
        effects.append("remote_message_created")
        try:
            await asyncio.Event().wait()
        finally:
            handler_stopped.set()

    async with TelegramClient(
        TOKEN, timeouts=RequestTimeouts(total=0.02), transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.send_photo_bytes(1, b"photo", "caption")
        assert result.state == "unknown_outcome" and result.code == "timeout"
        assert handler_stopped.is_set() and client.pending_count == 0
    assert effects == ["remote_message_created"]


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
async def test_connection_exception_after_response_headers_cannot_prove_not_sent(error_type):
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":'
            raise error_type(PRIVATE)

        async def aclose(self):
            closed.append(True)

    async with TelegramClient(
        TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))
    ) as client:
        result = await client.send_text(1, "text")
    assert result.state == "unknown_outcome" and result.code == "transport_error"
    assert closed == [True]


async def test_cancellation_before_dispatch_propagates_not_sent():
    calls = []
    async with TelegramClient(
        TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as client:
        task = asyncio.create_task(client.send_text(1, "text"))
        await asyncio.sleep(0)  # Public call has scheduled, but not dispatched, the owned request.
        task.cancel(PRIVATE)
        with pytest.raises(TelegramCancelled) as caught:
            await task
        assert caught.value.result.state == "not_sent"
        assert caught.value.result.code == "cancelled"
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    assert not calls


async def test_cancellation_after_dispatch_propagates_unknown_and_closes_files(
    tmp_path, monkeypatch
):
    image = tmp_path / "photo.png"
    image.write_bytes(b"synthetic")
    opened = []
    original_open = Path.open

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracked_open)
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(client.send_photo_paths(1, "caption", [image]))
        await entered.wait()
        task.cancel(PRIVATE)
        with pytest.raises(TelegramCancelled) as caught:
            await task
        assert isinstance(caught.value, asyncio.CancelledError)
        assert caught.value.result.state == "unknown_outcome"
        assert stopped.is_set() and all(handle.closed for handle in opened)
        assert TOKEN not in repr(caught.value) and PRIVATE not in str(caught.value)


async def test_partial_file_open_failure_closes_previous_files_without_sending(
    tmp_path, monkeypatch
):
    image = tmp_path / "first.png"
    image.write_bytes(b"synthetic")
    opened = []
    original_open = Path.open

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracked_open)
    calls = []
    async with TelegramClient(
        TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as client:
        result = await client.send_photo_paths(1, "caption", [image, tmp_path / "missing.png"])
    assert result.state == "not_sent" and result.code == "local_file_error"
    assert opened and all(handle.closed for handle in opened)
    assert not calls


async def test_repeated_cancellation_during_response_close_waits_for_cleanup_before_client_close():
    closing = asyncio.Event()
    release = asyncio.Event()
    order = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":true,"result":{"message_id":71}}'

        async def aclose(self):
            closing.set()
            await release.wait()
            order.append("response_closed")

    class Transport(httpx.MockTransport):
        async def aclose(self):
            order.append("transport_closed")

    client = TelegramClient(
        TOKEN, transport=Transport(lambda r: httpx.Response(200, stream=Stream()))
    )
    send = asyncio.create_task(client.send_text(1, "text"))
    await closing.wait()
    send.cancel(PRIVATE)
    await asyncio.sleep(0)
    send.cancel(PRIVATE)
    close = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    close.cancel(PRIVATE)
    await asyncio.sleep(0)
    assert not close.done() and not send.done() and not client.is_closed
    release.set()
    with pytest.raises(TelegramCancelled) as caught:
        await send
    assert caught.value.result.state == "sent" and caught.value.result.message_id == 71
    with pytest.raises(asyncio.CancelledError):
        await close
    assert client.is_closed and client.pending_count == 0
    assert order == ["response_closed", "transport_closed"]
    await client.aclose()
    assert (await client.send_text(1, "text")).code == "client_closed"


async def test_close_cancels_and_joins_all_inflight_requests():
    entered = asyncio.Event()
    starts = []
    stopped = []

    async def handler(request):
        starts.append(1)
        if len(starts) == 2:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(1)

    client = TelegramClient(TOKEN, transport=httpx.MockTransport(handler))
    tasks = [asyncio.create_task(client.send_text(1, "text")) for _ in range(2)]
    await entered.wait()
    assert client.pending_count == 2
    await client.aclose()
    for task in tasks:
        with pytest.raises(TelegramCancelled) as caught:
            await task
        assert caught.value.result.state == "unknown_outcome"
    assert len(stopped) == 2 and client.pending_count == 0 and client.is_closed


async def test_private_http_logs_are_scoped_and_raw_failures_are_not_exposed(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        logging.getLogger("httpx").info("unsafe %s", request.url)
        logging.getLogger("httpcore.proxy").debug("unsafe %s", PRIVATE)
        raise httpx.ReadError(f"{PRIVATE} {request.url}", request=request)

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.send_text(1, PRIVATE)
        logging.getLogger("httpx").info("unrelated request preserved")
        assert TOKEN not in repr(client) and PRIVATE not in repr(result)
    assert TOKEN not in caplog.text and PRIVATE not in caplog.text
    assert "api.telegram.org" not in caplog.text
    assert "unrelated request preserved" in caplog.text


async def test_success_httpx_log_is_suppressed_and_other_task_logging_is_not(caplog):
    caplog.set_level(logging.DEBUG)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return success()

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(client.send_text(1, PRIVATE))
        await entered.wait()
        logging.getLogger("httpcore.connection").debug("other task preserved")
        release.set()
        assert (await task).state == "sent"
    assert TOKEN not in caplog.text and "api.telegram.org" not in caplog.text
    assert "other task preserved" in caplog.text


@pytest.mark.parametrize("token", ["", "wrong", "1:token/path", "1:token?query", "1:token\n", None])
def test_invalid_token_cannot_change_origin_or_leak(token):
    with pytest.raises(TelegramConfigurationError) as caught:
        TelegramClient(token)
    assert caught.value.code == ConfigurationCode.INVALID_TOKEN
    assert str(caught.value) == "invalid_token"


@pytest.mark.parametrize(
    "value,code",
    [
        ("", "invalid_proxy"),
        ("http://host", "invalid_proxy"),
        ("http://host:0", "invalid_proxy"),
        ("http://host:65536", "invalid_proxy"),
        ("http://host:80/path", "invalid_proxy"),
        ("http://host:80?x=y", "invalid_proxy"),
        ("http://host:80#fragment", "invalid_proxy"),
        ("http://a_b:80", "invalid_proxy"),
        ("http://user:p@ss@host:80", "invalid_proxy"),
        ("http://user:%0A@host:80", "invalid_proxy"),
        ("http://user:%GG@host:80", "invalid_proxy"),
        (" http://host:80", "invalid_proxy"),
        ("socks5://host:1080", "unsupported_proxy"),
        ("host:80", "unsupported_proxy"),
    ],
)
def test_proxy_limits_never_silently_fall_back(value, code):
    with pytest.raises(TelegramConfigurationError) as caught:
        ProxySettings(value)
    assert str(caught.value) == code


async def test_proxy_tls_retries_environment_and_transport_ownership(monkeypatch):
    captured = []

    def make_transport(**kwargs):
        captured.append(kwargs)
        return httpx.MockTransport(lambda r: success())

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", make_transport)
    monkeypatch.setenv("HTTPS_PROXY", "http://unwanted.invalid:80")
    proxy = ProxySettings("https://user:synthetic%40password@[::1]:8443")
    async with TelegramClient(TOKEN, proxy=proxy) as client:
        assert (await client.send_text(1, "text")).state == "sent"
    assert captured[0]["proxy"] == proxy.url
    assert captured[0]["verify"] is True and captured[0]["trust_env"] is False
    assert captured[0]["retries"] == 0
    assert "password" not in repr(proxy)
    with pytest.raises(TelegramConfigurationError) as caught:
        TelegramClient(TOKEN, proxy=proxy, transport=httpx.MockTransport(lambda r: success()))
    assert caught.value.code == ConfigurationCode.PROXY_WITH_MOCK_TRANSPORT


@pytest.mark.parametrize("value", [0, -1, 121, float("inf"), float("nan"), True, "5", 10**1000])
def test_timeout_limits_are_bounded_and_type_checked(value):
    with pytest.raises(TelegramConfigurationError, match="^invalid_timeout$"):
        RequestTimeouts(total=value)


async def test_invalid_inputs_are_not_dispatched_and_empty_path_list_is_not_a_fallback():
    calls = []
    async with TelegramClient(
        TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as client:
        invalid = [
            await client.send_text(0, "text"),
            await client.send_text(True, "text"),
            await client.send_text(1, ""),
            await client.send_text(1, "text", thread_id="7:8"),
            await client.send_text(1, "text", thread_id=" 7 "),
            await client.send_text(1, "text", thread_id=0),
            await client.send_text(1, "\ud800"),
            await client.send_photo_paths(1, "caption", []),
            await client.send_photo_bytes(1, b"", "caption"),
            await client.send_photo_bytes(1, b"x", "caption", reply_to_message_id=True),
            await client.edit_text(1, 0, "text"),
        ]
    assert all(item.state == "not_sent" and item.code == "invalid_input" for item in invalid)
    assert not calls


async def test_response_size_and_unexpected_compression_are_unknown():
    responses = [
        httpx.Response(200, content=b"x" * (65536 + 1)),
        httpx.Response(
            200,
            content=b'{"ok":true,"result":{"message_id":1}}',
            headers={"content-encoding": "custom"},
        ),
    ]
    async with TelegramClient(
        TOKEN, transport=httpx.MockTransport(lambda r: responses.pop(0))
    ) as client:
        result = await client.send_text(1, "text")
        assert result.state == "unknown_outcome" and result.code == "response_too_large"
        result = await client.send_text(1, "text")
        assert result.state == "unknown_outcome" and result.code == "invalid_response"
