import pytest

from fsm_panel.notifications.legacy import (
    INACTIVITY_MARKER,
    OLD_INACTIVITY_FILTER,
    BytesPhotoPart,
    ItemLine,
    LegacyDestination,
    LegacyInputError,
    LegacyOperation,
    NotificationState,
    PathPhotoPart,
    accepted_legacy_receipt,
    build_legacy_request,
    evaluate_legacy_response,
    format_item_message,
    important_decision,
    inactivity_decision,
    parse_legacy_proxy,
    parse_receipt_destination,
    parse_threaded_destination,
    producer_repeats_fanout,
    receipt_destination_components,
    receipt_first_kind,
    receipt_text_fallback,
    selector_first_kind,
    selector_text_fallback,
    threaded_destination_components,
)


def test_destination_paths_keep_distinct_whitespace_empty_and_colon_rules():
    raw = " -10: 7 , ,+1_2:3:4,"
    assert threaded_destination_components(raw) == (" -10: 7 ", " ", "+1_2:3:4", "")
    assert receipt_destination_components(raw) == ("-10: 7", "+1_2:3:4")
    assert parse_threaded_destination(" -10: 7 ") == LegacyDestination(-10, " 7 ")
    assert parse_receipt_destination("+1_2:3:4") == LegacyDestination(12, "3:4")
    assert parse_threaded_destination(" １２ ") == LegacyDestination(12, None)
    assert parse_threaded_destination("1:") == LegacyDestination(1, "")
    with pytest.raises(LegacyInputError, match="^invalid_legacy_input$"):
        parse_threaded_destination("+1_2:3:4")
    with pytest.raises(LegacyInputError):
        parse_receipt_destination("")


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        (" ", None),
        ("host", None),
        (":8080", None),
        (" host:8080 ", "http://host:8080"),
        ("host:not-a-port", "http://host:not-a-port"),
        ("host:99999:ignored", "http://host:99999"),
        ("host:8080:user:pass:with:colons", "http://user:pass:with:colons@host:8080"),
        ("user:pass:with:colons@host:8080", "http://user:pass:with:colons@host:8080"),
        ("host:8080@user:pass", "http://user:pass@host:8080"),
        ("user:@host:8080", "http://user@host:8080"),
        ("user@host:service", "http://user@host:service"),
        ("user:10@host:20", "http://user:10@host:20"),
        ("user@host:１２", "http://user@host:１２"),
        ("SOCKS5://host:1080", "SOCKS5://host:1080"),
        ("socksWhatever://host:1", "socksWhatever://host:1"),
        ("http:// user : p @ host : 80 ", "http:// user : p @ host : 80"),
    ],
)
def test_proxy_characterization(value, expected):
    result = parse_legacy_proxy(value)
    if expected is None:
        assert result is None
    else:
        assert dict(result.proxies) == {"http": expected, "https": expected}
        assert expected not in repr(result)
        with pytest.raises(TypeError):
            result.proxies["http"] = "changed"


def test_bad_proxy_error_does_not_include_credentials():
    with pytest.raises(LegacyInputError) as caught:
        parse_legacy_proxy("user:synthetic-password@bad@host:80")
    assert "synthetic-password" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "operation,method,timeout,field,html,topic",
    [
        (LegacyOperation.RECEIPT_PHOTO, "sendPhoto", 40, "caption", True, True),
        (LegacyOperation.RECEIPT_TEXT, "sendMessage", 20, "text", True, True),
        (LegacyOperation.EDIT_CAPTION, "editMessageCaption", 20, "caption", True, False),
        (LegacyOperation.EDIT_TEXT, "editMessageText", 20, "text", True, False),
        (LegacyOperation.LEGACY_PHOTO, "sendPhoto", 40, "caption", True, True),
        (LegacyOperation.LEGACY_TEXT, "sendMessage", 15, "text", True, True),
        (LegacyOperation.BOT_TEXT, "sendMessage", 30, "text", False, False),
        (LegacyOperation.BOT_PHOTO_BYTES, "sendPhoto", 30, "caption", False, False),
    ],
)
def test_eight_exact_form_request_contracts(operation, method, timeout, field, html, topic):
    plan = build_legacy_request(
        operation,
        chat_id=-101,
        message="<b>raw & message</b>",
        thread_id="8:9",
        message_id=50,
        images=("secret-path.jpg", "other.webp"),
        photo_bytes=b"private-photo",
        reply_to_message_id=60,
    )
    assert plan.method == method
    assert plan.timeout == timeout
    assert plan.encoding == "form" and plan.verify_tls is False
    expected = {"chat_id": -101, field: "<b>raw & message</b>"}
    if html:
        expected["parse_mode"] = "HTML"
    if topic:
        expected["message_thread_id"] = "8:9"
    if operation in (LegacyOperation.EDIT_TEXT, LegacyOperation.EDIT_CAPTION):
        expected["message_id"] = 50
    if operation == LegacyOperation.BOT_PHOTO_BYTES:
        expected["reply_to_message_id"] = 60
        assert plan.files == (BytesPhotoPart(b"private-photo"),)
    elif method == "sendPhoto":
        assert plan.files == (PathPhotoPart("secret-path.jpg"), PathPhotoPart("other.webp"))
    else:
        assert plan.files == ()
    assert dict(plan.fields) == expected
    assert "raw & message" not in repr(plan)
    assert "secret-path" not in repr(plan.files)
    assert "private-photo" not in repr(plan.files)


def test_empty_topic_and_reply_are_not_emitted_and_empty_path_list_stays_empty():
    plan = build_legacy_request(LegacyOperation.LEGACY_PHOTO, chat_id=1, message="", thread_id="")
    assert dict(plan.fields) == {"chat_id": 1, "caption": "", "parse_mode": "HTML"}
    assert plan.files == ()
    bot = build_legacy_request(
        LegacyOperation.BOT_PHOTO_BYTES, chat_id=1, message="", reply_to_message_id=0
    )
    assert dict(bot.fields) == {"chat_id": 1, "caption": ""}


@pytest.mark.parametrize("decoded", [{"ok": False}, [], None, True, 0, "anything"])
def test_legacy_photo_accepts_any_decoded_json(decoded):
    assert evaluate_legacy_response(LegacyOperation.LEGACY_PHOTO, decoded).value is True
    assert not selector_text_fallback(True)


def test_receipt_truthiness_and_missing_id_error_path():
    for identifier in (0, False, "", "noninteger"):
        result = evaluate_legacy_response(
            LegacyOperation.RECEIPT_TEXT, {"ok": "truthy", "result": {"message_id": identifier}}
        )
        assert result.value is identifier
        assert not result.caught_exception
    assert evaluate_legacy_response(LegacyOperation.RECEIPT_TEXT, {"ok": False}).value is None
    for malformed in ({"ok": True}, {"ok": True, "result": {}}, None):
        result = evaluate_legacy_response(LegacyOperation.RECEIPT_PHOTO, malformed)
        assert result.value is None and result.caught_exception
        assert result.logging == "console"
    assert evaluate_legacy_response(LegacyOperation.EDIT_TEXT, {"ok": "yes"}).value is True


@pytest.mark.parametrize("operation", list(LegacyOperation))
def test_legacy_exception_and_response_policies(operation):
    result = evaluate_legacy_response(operation, ordinary_exception=True)
    propagates = operation in {
        LegacyOperation.LEGACY_TEXT,
        LegacyOperation.BOT_TEXT,
        LegacyOperation.BOT_PHOTO_BYTES,
    }
    assert result.exception_propagates is propagates
    assert result.caught_exception is not propagates
    if operation == LegacyOperation.LEGACY_PHOTO:
        assert result.value is False and result.logging == "console_and_error_file"
    decoded = {"private": "synthetic"}
    if operation in {LegacyOperation.BOT_TEXT, LegacyOperation.BOT_PHOTO_BYTES}:
        assert evaluate_legacy_response(operation, decoded).value is decoded
    if operation == LegacyOperation.LEGACY_TEXT:
        assert evaluate_legacy_response(operation, decoded).value is None


def test_identity_based_receipt_fallback_differs_from_boolean_selector():
    assert selector_first_kind([]) == "photo"
    assert receipt_first_kind([]) == "text"
    assert selector_first_kind(None) == "text"
    assert receipt_first_kind(["a"]) == "photo"
    destination = LegacyDestination(1, "2")
    for value in (0, False, ""):
        assert selector_text_fallback(value)
        assert not receipt_text_fallback(value)
        assert accepted_legacy_receipt(destination, value, "photo").message_id is value
    assert receipt_text_fallback(None)
    assert accepted_legacy_receipt(destination, None, "text") is None
    assert producer_repeats_fanout(None)  # Normal legacy daemon fanout return.


def test_inactivity_updates_before_delivery_and_is_not_a_timer():
    previous = NotificationState(f"prefix {INACTIVITY_MARKER} for 2 hours", 10)
    suppressed = inactivity_decision(previous, INACTIVITY_MARKER, repeating=False, now=100000)
    assert suppressed.suppressed and suppressed.state_before_delivery is previous
    allowed = inactivity_decision(previous, INACTIVITY_MARKER, repeating="false", now=20)
    assert not allowed.suppressed
    assert allowed.state_before_delivery == NotificationState(INACTIVITY_MARKER, 20)
    assert not inactivity_decision(
        previous, INACTIVITY_MARKER.lower(), repeating=False, now=30
    ).suppressed


def test_important_filter_expansion_case_sensitivity_and_empty_substring():
    original = [OLD_INACTIVITY_FILTER]
    result = important_decision(original, INACTIVITY_MARKER + " for 3 hours")
    assert result.matched and result.expanded_filter == (OLD_INACTIVITY_FILTER, INACTIVITY_MARKER)
    assert original == [OLD_INACTIVITY_FILTER]
    assert (
        important_decision(result.expanded_filter, "none").expanded_filter == result.expanded_filter
    )
    assert not important_decision(["Error"], "error").matched
    assert important_decision([""], "anything").matched
    assert not important_decision([], "anything").matched


def test_item_formatter_retains_raw_html_price_first_and_exact_newlines():
    assert format_item_message("prefix", []) == "prefix"
    assert format_item_message(
        "prefix", [ItemLine("<b>A&B</b>", "<5"), ItemLine("B", "unknown")]
    ) == ("prefix\n\n<5 | <b>A&B</b>\nunknown | B\n")
