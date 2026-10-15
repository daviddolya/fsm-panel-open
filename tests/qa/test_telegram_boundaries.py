"""Independent Telegram boundaries: static legacy fixtures and offline HTTP only."""

import asyncio
import logging
import socket
import traceback
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from fsm_panel.notifications import (
    ConfigurationCode,
    DeliveryCode,
    DeliveryState,
    RequestTimeouts,
    TelegramCancelled,
    TelegramClient,
    TelegramConfigurationError,
    legacy,
)

TOKEN = "123456789:QA_SYNTHETIC_TELEGRAM_TOKEN"
MESSAGE = "QA_PRIVATE_MESSAGE_<b>unchanged & raw</b>"
PRIVATE = "QA_PRIVATE_EXCEPTION_OR_RESPONSE"
CHAT = -1000123456789


@pytest.mark.parametrize(
    "supplied,expected",
    [
        (None, None),
        ("   ", None),
        ("host:0", "http://host:0"),
        ("host:wrong-port", "http://host:wrong-port"),
        ("host:9:ignored", "http://host:9"),
        ("host:9:user:", "http://user@host:9"),
        ("host:9:user:pass:with:colons", "http://user:pass:with:colons@host:9"),
        ("user:@host:9", "http://user@host:9"),
        ("@host:9", "http://host:9"),
        ("user:9@host:8", "http://user:9@host:8"),
        ("host:8@user:pass:more", "http://user:pass:more@host:8"),
        ("user:pass@host:other", "http://user:pass@host:other"),
        ("user:pass@host", None),
        ("SOCKS5://user:pass@host:9", "SOCKS5://user:pass@host:9"),
        (" custom:// host :x ", "custom:// host :x"),
        ("user:pass@[::1]:9", "http://user:pass@[::1]:9"),
        ("[::1]:9", None),
    ],
)
def test_legacy_proxy_branch_table_preserves_unvalidated_literal_rules(supplied, expected):
    result = legacy.parse_legacy_proxy(supplied)
    if expected is None:
        assert result is None
    else:
        assert dict(result.proxies) == {"http": expected, "https": expected}
        assert supplied not in repr(result)
        with pytest.raises(TypeError):
            result.proxies["http"] = "changed"


def test_legacy_destinations_preserve_distinct_comma_colon_and_topic_rules():
    text = " , +0012:007 ,, -9:0,"
    assert legacy.threaded_destination_components(text) == (" ", " +0012:007 ", "", " -9:0", "")
    assert legacy.receipt_destination_components(text) == ("+0012:007", "-9:0")
    threaded = legacy.parse_threaded_destination(" +0012:007 ")
    assert (threaded.chat_id, threaded.thread_id) == (12, "007 ")
    receipt = legacy.parse_receipt_destination("+0012:007:009")
    assert (receipt.chat_id, receipt.thread_id) == (12, "007:009")
    with pytest.raises(legacy.LegacyInputError, match="invalid_legacy_input"):
        legacy.parse_threaded_destination("+0012:007:009")
    with pytest.raises(legacy.LegacyInputError) as caught:
        legacy.parse_threaded_destination(PRIVATE)
    assert PRIVATE not in repr(caught.value)
    proxy_error = "user:pass@host:9@another"
    with pytest.raises(legacy.LegacyInputError) as caught:
        legacy.parse_legacy_proxy(proxy_error)
    assert proxy_error not in repr(caught.value)


@pytest.mark.parametrize("message_id", [0, False, "", [], {}])
def test_falsey_non_none_legacy_receipts_are_accepted_and_do_not_fallback(message_id):
    decoded = {"ok": "false", "result": {"message_id": message_id}}
    destination = legacy.LegacyDestination(CHAT, "private-topic")
    for operation in (legacy.LegacyOperation.RECEIPT_PHOTO, legacy.LegacyOperation.RECEIPT_TEXT):
        result = legacy.evaluate_legacy_response(operation, decoded)
        assert result.value is message_id and not result.caught_exception
        assert legacy.receipt_text_fallback(result.value) is False
        receipt = legacy.accepted_legacy_receipt(destination, result.value, "photo")
        assert receipt is not None and receipt.message_id is message_id
        assert str(CHAT) not in repr(receipt) and "private-topic" not in repr(receipt)


def test_legacy_fallback_and_producer_repeat_do_not_prove_that_nothing_was_sent():
    assert legacy.selector_first_kind([]) == "photo"
    assert legacy.receipt_first_kind([]) == "text"
    assert legacy.selector_first_kind(None) == legacy.receipt_first_kind(None) == "text"
    for decoded in (None, [], False, {"ok": False, "description": PRIVATE}):
        photo = legacy.evaluate_legacy_response(legacy.LegacyOperation.LEGACY_PHOTO, decoded)
        assert photo.value is True and not photo.caught_exception
        assert legacy.selector_text_fallback(photo.value) is False
    failed = legacy.evaluate_legacy_response(
        legacy.LegacyOperation.LEGACY_PHOTO, ordinary_exception=True
    )
    assert failed.value is False and failed.logging == "console_and_error_file"
    assert legacy.selector_text_fallback(failed.value) is True
    malformed = legacy.evaluate_legacy_response(
        legacy.LegacyOperation.RECEIPT_PHOTO, {"ok": True, "result": {}}
    )
    assert malformed.caught_exception and legacy.receipt_text_fallback(malformed.value)
    assert legacy.producer_repeats_fanout(None) is True
    for operation in (
        legacy.LegacyOperation.LEGACY_TEXT,
        legacy.LegacyOperation.BOT_TEXT,
        legacy.LegacyOperation.BOT_PHOTO_BYTES,
    ):
        assert legacy.evaluate_legacy_response(
            operation, ordinary_exception=True
        ).exception_propagates


@pytest.mark.parametrize(
    "operation,method,timeout,html,topic",
    [
        (legacy.LegacyOperation.RECEIPT_PHOTO, "sendPhoto", 40, True, True),
        (legacy.LegacyOperation.RECEIPT_TEXT, "sendMessage", 20, True, True),
        (legacy.LegacyOperation.LEGACY_PHOTO, "sendPhoto", 40, True, True),
        (legacy.LegacyOperation.LEGACY_TEXT, "sendMessage", 15, True, True),
        (legacy.LegacyOperation.EDIT_CAPTION, "editMessageCaption", 20, True, False),
        (legacy.LegacyOperation.EDIT_TEXT, "editMessageText", 20, True, False),
        (legacy.LegacyOperation.BOT_TEXT, "sendMessage", 30, False, False),
        (legacy.LegacyOperation.BOT_PHOTO_BYTES, "sendPhoto", 30, False, False),
    ],
)
def test_eight_legacy_form_contracts_do_not_infer_photo_mime_or_topic_fields(
    monkeypatch, operation, method, timeout, html, topic
):
    def forbidden(*args, **kwargs):
        pytest.fail("legacy characterization attempted IO")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    result = legacy.build_legacy_request(
        operation,
        chat_id=CHAT,
        message=MESSAGE,
        message_id=23,
        thread_id="0",
        images=("private-one.jpg", "private-two.bin"),
        photo_bytes=b"private bytes",
        reply_to_message_id=24,
    )
    assert result.method == method and result.timeout == timeout
    assert result.encoding == "form" and result.verify_tls is False
    assert (result.fields.get("parse_mode") == "HTML") is html
    assert (result.fields.get("message_thread_id") == "0") is topic
    assert MESSAGE in result.fields.values()
    if operation in (legacy.LegacyOperation.RECEIPT_PHOTO, legacy.LegacyOperation.LEGACY_PHOTO):
        assert [(part.field_name, part.filename, part.content_type) for part in result.files] == [
            ("photo", "photo", "image/png"),
            ("photo", "photo", "image/png"),
        ]
        assert "reply_to_message_id" not in result.fields
    elif operation == legacy.LegacyOperation.BOT_PHOTO_BYTES:
        assert result.fields["reply_to_message_id"] == 24
        assert result.files[0].filename is None and result.files[0].content_type is None
    for private in (MESSAGE, str(CHAT), "private bytes", "private-one.jpg"):
        assert private not in repr(result)


def test_inactivity_and_important_filter_decisions_are_literal_and_update_before_delivery():
    marker = "Panel has not sent any messages"
    prior = legacy.NotificationState(marker + " for 2 hours", 50)
    suppressed = legacy.inactivity_decision(prior, "prefix " + marker, repeating=False, now=100)
    assert suppressed.suppressed and suppressed.state_before_delivery is prior
    repeated = legacy.inactivity_decision(prior, marker, repeating="false", now=100)
    assert not repeated.suppressed and repeated.state_before_delivery.last_time == 100
    changed_case = legacy.inactivity_decision(prior, marker.lower(), repeating=False, now=101)
    assert not changed_case.suppressed and changed_case.state_before_delivery.last_time == 101
    old_filter = [marker + " for the last 2 hours"]
    result = legacy.important_decision(old_filter, marker + " for a different interval")
    assert result.matched and result.expanded_filter == (*old_filter, marker)
    assert old_filter == [marker + " for the last 2 hours"]
    assert (
        legacy.important_decision(result.expanded_filter, marker).expanded_filter
        == result.expanded_filter
    )
    assert legacy.important_decision([""], MESSAGE).matched
    assert not legacy.important_decision([], MESSAGE).matched
    assert not legacy.important_decision([marker], marker.lower()).matched
    assert legacy.format_item_message("prefix", []) == "prefix"
    assert (
        legacy.format_item_message(
            "prefix",
            [legacy.ItemLine("<b>name</b>", "<i>price</i>"), legacy.ItemLine("name2", "unknown")],
        )
        == "prefix\n\n<i>price</i> | <b>name</b>\nunknown | name2\n"
    )


@pytest.mark.parametrize(
    "status,body,expected_state,expected_code",
    [
        (
            200,
            b"not JSON " + PRIVATE.encode(),
            DeliveryState.UNKNOWN_OUTCOME,
            DeliveryCode.INVALID_RESPONSE,
        ),
        (
            200,
            b'{"ok":true,"result":{}}',
            DeliveryState.UNKNOWN_OUTCOME,
            DeliveryCode.INVALID_RESPONSE,
        ),
        (
            200,
            b'{"ok":true,"result":{"message_id":true}}',
            DeliveryState.UNKNOWN_OUTCOME,
            DeliveryCode.INVALID_RESPONSE,
        ),
        (
            200,
            b'{"ok":"true","result":{"message_id":1}}',
            DeliveryState.UNKNOWN_OUTCOME,
            DeliveryCode.INVALID_RESPONSE,
        ),
        (200, b"[]", DeliveryState.UNKNOWN_OUTCOME, DeliveryCode.INVALID_RESPONSE),
        (
            503,
            b'{"ok":true,"result":{"message_id":1}}',
            DeliveryState.UNKNOWN_OUTCOME,
            DeliveryCode.HTTP_STATUS,
        ),
        (
            302,
            b'{"ok":true,"result":{"message_id":1}}',
            DeliveryState.UNKNOWN_OUTCOME,
            DeliveryCode.HTTP_STATUS,
        ),
        (
            429,
            b'{"ok":false,"error_code":429,"parameters":{"retry_after":0}}',
            DeliveryState.REJECTED,
            DeliveryCode.REMOTE_REJECTED,
        ),
    ],
)
async def test_unusable_or_rejected_responses_never_retry_or_follow_redirects(
    status, body, expected_state, expected_code, caplog
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, content=body, headers={"Location": "https://synthetic.invalid/"}
        )

    caplog.set_level(logging.DEBUG)
    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.send_text(CHAT, MESSAGE, thread_id="007")
    assert result.state == expected_state and result.code == expected_code
    assert len(requests) == 1 and requests[0].method == "POST"
    assert requests[0].url.host == "api.telegram.org"
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert parse_qs(requests[0].content.decode()) == {
        "chat_id": [str(CHAT)],
        "text": [MESSAGE],
        "parse_mode": ["HTML"],
        "message_thread_id": ["007"],
    }
    for private in (TOKEN, MESSAGE, PRIVATE):
        assert private not in repr(result) + caplog.text


@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError]
)
async def test_photo_transport_ambiguity_does_not_trigger_text_fallback(
    tmp_path, monkeypatch, error_type, caplog
):
    paths = [tmp_path / name for name in ("private-one.jpg", "private-two.bin")]
    for index, path in enumerate(paths):
        path.write_bytes(f"private synthetic image {index}".encode())
    opened, requests = [], []
    native_open = Path.open

    def tracked_open(path, *args, **kwargs):
        handle = native_open(path, *args, **kwargs)
        if path in paths:
            opened.append(handle)
        return handle

    def handler(request):
        requests.append(request)
        raise error_type(TOKEN + PRIVATE, request=request)

    monkeypatch.setattr(Path, "open", tracked_open)
    caplog.set_level(logging.DEBUG)
    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.send_photo_paths(CHAT, MESSAGE, paths)
    assert result.state == DeliveryState.UNKNOWN_OUTCOME
    assert len(requests) == 1 and requests[0].url.path.endswith("/sendPhoto")
    assert requests[0].content.count(b'name="photo"; filename="photo"') == 2
    assert requests[0].content.count(b"Content-Type: image/png") == 2
    assert len(opened) == 2 and all(handle.closed for handle in opened)
    for private in (TOKEN, PRIVATE, MESSAGE, "private-one.jpg"):
        assert private not in repr(result) + caplog.text


async def test_partial_local_photo_open_failure_closes_prior_files_without_sending(
    tmp_path, monkeypatch
):
    first, missing = tmp_path / "first-private-image", tmp_path / "missing-private-image"
    first.write_bytes(b"synthetic image")
    opened = []
    native_open = Path.open

    def tracked_open(path, *args, **kwargs):
        result = native_open(path, *args, **kwargs)
        if path == first:
            opened.append(result)
        return result

    def forbidden(request):
        pytest.fail("local file failure still sent a request")

    monkeypatch.setattr(Path, "open", tracked_open)
    async with TelegramClient(TOKEN, transport=httpx.MockTransport(forbidden)) as client:
        result = await client.send_photo_paths(CHAT, MESSAGE, [first, missing])
    assert result.state == DeliveryState.NOT_SENT and result.code == DeliveryCode.LOCAL_FILE_ERROR
    assert len(opened) == 1 and opened[0].closed
    assert str(missing) not in repr(result)


async def test_cancellation_before_dispatch_has_no_remote_effect():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        sending = asyncio.create_task(client.send_text(CHAT, MESSAGE))
        await asyncio.sleep(0)
        sending.cancel()
        with pytest.raises(TelegramCancelled) as caught:
            await sending
        assert caught.value.result.state == DeliveryState.NOT_SENT
        assert caught.value.result.code == DeliveryCode.CANCELLED
        assert client.pending_count == 0
    assert requests == []


class DelayedCloseStream(httpx.AsyncByteStream):
    def __init__(self):
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0
        self.closed = False
        self.close_cancelled = False

    async def __aiter__(self):
        yield b'{"ok":true,"result":{"message_id":77}}'

    async def aclose(self):
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.closed = True


async def test_repeated_cancellation_joins_eof_cleanup_and_retains_known_success():
    stream = DelayedCloseStream()
    transport_closed = []

    class ObservedTransport(httpx.MockTransport):
        async def aclose(self):
            transport_closed.append(stream.closed)
            await super().aclose()

    client = TelegramClient(
        TOKEN, transport=ObservedTransport(lambda _: httpx.Response(200, stream=stream))
    )
    sending = asyncio.create_task(client.send_text(CHAT, MESSAGE))
    await asyncio.wait_for(stream.close_started.wait(), 2)
    try:
        sending.cancel()
        await asyncio.sleep(0)
        sending.cancel()
        await asyncio.sleep(0)
        closing = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        both_waited = not sending.done() and not closing.done()
        transport_closed_early = bool(transport_closed)
        refused = await client.send_text(CHAT, "new work while closing")
    finally:
        stream.release_close.set()
    with pytest.raises(TelegramCancelled) as caught:
        await asyncio.wait_for(sending, 2)
    await asyncio.wait_for(closing, 2)
    await client.aclose()
    assert caught.value.result.state == DeliveryState.SENT
    assert caught.value.result.message_id == 77
    assert both_waited and not transport_closed_early
    assert refused.state == DeliveryState.NOT_SENT and refused.code == DeliveryCode.CLIENT_CLOSED
    assert stream.closed and not stream.close_cancelled and stream.close_calls == 1
    assert transport_closed == [True]
    assert client.is_closed and client.pending_count == 0


async def test_failure_after_response_headers_can_never_claim_request_was_not_sent():
    class FailedBody(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False

        async def __aiter__(self):
            yield b'{"ok":true,'
            raise httpx.ConnectError(PRIVATE)

        async def aclose(self):
            self.closed = True

    stream = FailedBody()
    async with TelegramClient(
        TOKEN, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as client:
        result = await client.send_text(CHAT, MESSAGE)
    assert result.state == DeliveryState.UNKNOWN_OUTCOME
    assert stream.closed and PRIVATE not in repr(result)


async def test_total_deadline_after_dispatch_is_ambiguous_and_does_not_repeat():
    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.Event().wait()

    async with TelegramClient(
        TOKEN, transport=httpx.MockTransport(handler), timeouts=RequestTimeouts(total=0.02)
    ) as client:
        result = await client.send_text(CHAT, MESSAGE)
        assert client.pending_count == 0
    assert result.state == DeliveryState.UNKNOWN_OUTCOME and result.code == DeliveryCode.TIMEOUT
    assert len(requests) == 1


async def test_request_logging_filter_is_scoped_to_the_owned_request_context(caplog):
    entered, release = asyncio.Event(), asyncio.Event()
    logger = logging.getLogger("httpx")

    async def handler(request):
        logger.warning(TOKEN + PRIVATE)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    caplog.set_level(logging.DEBUG)
    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        pending = asyncio.create_task(client.send_text(CHAT, MESSAGE))
        await asyncio.wait_for(entered.wait(), 2)
        logger.warning("unrelated task still observable")
        release.set()
        result = await pending
    assert result.state == DeliveryState.SENT
    assert "unrelated task still observable" in caplog.text
    assert TOKEN not in caplog.text and PRIVATE not in caplog.text


async def test_closing_one_client_only_cancels_its_owned_inflight_request():
    entered_a, entered_b, release_b = (asyncio.Event() for _ in range(3))
    close_order = []

    async def handler_a(request):
        entered_a.set()
        await asyncio.Event().wait()

    async def handler_b(request):
        entered_b.set()
        await release_b.wait()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 88}})

    class Transport(httpx.MockTransport):
        def __init__(self, handler, name):
            super().__init__(handler)
            self.name = name

        async def aclose(self):
            close_order.append(self.name)
            await super().aclose()

    first = TelegramClient(TOKEN, transport=Transport(handler_a, "first"))
    second = TelegramClient(TOKEN, transport=Transport(handler_b, "second"))
    request_a = asyncio.create_task(first.send_text(CHAT, MESSAGE))
    request_b = asyncio.create_task(second.send_text(CHAT, MESSAGE))
    try:
        await asyncio.wait_for(entered_a.wait(), 2)
        await asyncio.wait_for(entered_b.wait(), 2)
        await asyncio.wait_for(first.aclose(), 2)
        with pytest.raises(TelegramCancelled) as caught:
            await request_a
        assert caught.value.result.state == DeliveryState.UNKNOWN_OUTCOME
        assert not request_b.done() and second.pending_count == 1
        assert close_order == ["first"]
        release_b.set()
        result = await request_b
        assert result.state == DeliveryState.SENT and result.message_id == 88
    finally:
        release_b.set()
        await first.aclose()
        await second.aclose()
    assert close_order == ["first", "second"]
    assert first.pending_count == second.pending_count == 0


async def test_cancelled_close_waiter_still_joins_owned_cleanup_before_propagating():
    stream = DelayedCloseStream()
    client = TelegramClient(
        TOKEN, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    )
    sending = asyncio.create_task(client.send_text(CHAT, MESSAGE))
    await asyncio.wait_for(stream.close_started.wait(), 2)
    closing = asyncio.create_task(client.aclose())
    try:
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        still_waited = not closing.done() and not sending.done() and not client.is_closed
    finally:
        stream.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 2)
    with pytest.raises(TelegramCancelled) as caught:
        await asyncio.wait_for(sending, 2)
    await client.aclose()
    assert still_waited and client.is_closed and client.pending_count == 0
    assert stream.closed and stream.close_calls == 1 and not stream.close_cancelled
    assert caught.value.result.state == DeliveryState.SENT


async def test_two_explicit_calls_are_two_attempts_without_hidden_fanout_or_deduplication():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests)}})

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        first = await client.send_text(CHAT, MESSAGE)
        second = await client.send_text(CHAT, MESSAGE)
    assert first.message_id == 1 and second.message_id == 2
    assert len(requests) == 2 and requests[0].content == requests[1].content


async def test_explicit_photo_rejection_does_not_activate_legacy_text_fallback():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": PRIVATE + TOKEN}
        )

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.send_photo_bytes(
            CHAT, b"synthetic bytes", MESSAGE, reply_to_message_id=7
        )
    assert result.state == DeliveryState.REJECTED and result.code == DeliveryCode.REMOTE_REJECTED
    assert result.error_code == 400
    assert len(requests) == 1 and requests[0].url.path.endswith("/sendPhoto")
    assert b'name="reply_to_message_id"' in requests[0].content
    assert b"message_thread_id" not in requests[0].content
    assert b"parse_mode" not in requests[0].content
    assert TOKEN not in repr(result) and PRIVATE not in repr(result)


@pytest.mark.parametrize("returned", [78, True, None])
async def test_edit_receipts_must_identify_the_requested_message(returned):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": returned}})

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.edit_caption(CHAT, 77, MESSAGE)
    assert result.state == DeliveryState.UNKNOWN_OUTCOME
    assert result.code == DeliveryCode.INVALID_RESPONSE
    assert len(requests) == 1
    assert parse_qs(requests[0].content.decode()) == {
        "chat_id": [str(CHAT)],
        "message_id": ["77"],
        "caption": [MESSAGE],
        "parse_mode": ["HTML"],
    }


@pytest.mark.parametrize("overflow", [False, True])
async def test_response_limit_counts_chunks_and_closes_without_reading_after_overflow(overflow):
    prefix = b'{"ok":true,"result":{"message_id":71}}'
    visited = []

    class ChunkedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield prefix
            yield b" " * (65536 - len(prefix) + overflow)
            visited.append("read_after_limit")

        async def aclose(self):
            visited.append("closed")

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=ChunkedBody())

    async with TelegramClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        result = await client.send_text(CHAT, MESSAGE)
    assert len(requests) == 1
    if overflow:
        assert result.state == DeliveryState.UNKNOWN_OUTCOME
        assert result.code == DeliveryCode.RESPONSE_TOO_LARGE
        assert visited == ["closed"]
    else:
        assert result.state == DeliveryState.SENT and result.message_id == 71
        assert visited == ["read_after_limit", "closed"]


async def test_cleanup_failure_and_logs_remain_content_free_and_do_not_erase_known_result(caplog):
    caplog.set_level(logging.DEBUG)
    response_closed, transport_closed = [], []

    class PrivateCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":true,"result":{"message_id":71}}'

        async def aclose(self):
            logging.getLogger("httpcore.http11").warning(TOKEN + PRIVATE)
            response_closed.append(True)
            raise RuntimeError(TOKEN + PRIVATE)

    class FailedCloseTransport(httpx.MockTransport):
        async def aclose(self):
            logging.getLogger("httpcore.connection").warning(TOKEN + PRIVATE)
            transport_closed.append(True)
            raise RuntimeError(TOKEN + PRIVATE)

    client = TelegramClient(
        TOKEN,
        transport=FailedCloseTransport(lambda _: httpx.Response(200, stream=PrivateCloseStream())),
    )
    result = await client.send_text(CHAT, MESSAGE)
    assert result.state == DeliveryState.SENT and result.message_id == 71
    for _ in range(2):
        with pytest.raises(TelegramConfigurationError) as caught:
            await client.aclose()
        assert caught.value.code == ConfigurationCode.CLIENT_CLOSE_FAILED
        public_error = "".join(traceback.format_exception(caught.value))
        assert TOKEN not in public_error and PRIVATE not in public_error
    logging.getLogger("httpcore.connection").warning("unrelated cleanup context preserved")
    assert response_closed == [True] and transport_closed == [True]
    assert not client.is_closed and client.pending_count == 0
    assert TOKEN not in repr(client) + repr(result) + caplog.text
    assert PRIVATE not in caplog.text
    assert "unrelated cleanup context preserved" in caplog.text
