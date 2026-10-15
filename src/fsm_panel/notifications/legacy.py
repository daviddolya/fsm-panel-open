"""Pure characterization of the selected FSM_PANEL 3.1.2 Telegram bodies.

These helpers perform no I/O and must not drive retry decisions in the new
transport. They deliberately retain legacy truthiness, raw HTML and fallback
quirks. See docs/telegram.md for evidence and stricter public input boundaries.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

INACTIVITY_MARKER = "Panel has not sent any messages"
OLD_INACTIVITY_FILTER = "Panel has not sent any messages for the last 2 hours"


class LegacyInputError(ValueError):
    """Content-free boundary error, replacing unsafe native exception text."""

    def __init__(self) -> None:
        super().__init__("invalid_legacy_input")


def _string(value: object) -> None:
    if type(value) is not str:
        raise LegacyInputError()


@dataclass(frozen=True)
class LegacyDestination:
    chat_id: int = field(repr=False)
    thread_id: str | None = field(repr=False)


def threaded_destination_components(value: str) -> tuple[str, ...]:
    _string(value)
    return tuple(value.split(","))


def receipt_destination_components(value: str) -> tuple[str, ...]:
    _string(value)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_destination(value: str, *, receipt: bool) -> LegacyDestination:
    _string(value)
    try:
        thread_id = None
        if ":" in value:
            value, thread_id = value.split(":", 1) if receipt else value.split(":")
        return LegacyDestination(int(value), thread_id)
    except ValueError:
        raise LegacyInputError() from None


def parse_threaded_destination(value: str) -> LegacyDestination:
    return _parse_destination(value, receipt=False)


def parse_receipt_destination(value: str) -> LegacyDestination:
    return _parse_destination(value, receipt=True)


@dataclass(frozen=True)
class LegacyProxy:
    """The exact Requests mapping is private data, excluded from repr."""

    proxies: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "proxies", MappingProxyType(dict(self.proxies)))


def parse_legacy_proxy(proxy_str: str | None) -> LegacyProxy | None:
    """Port validity and scheme support are intentionally *not* checked here."""
    if proxy_str is None or proxy_str == "":
        return None
    _string(proxy_str)
    value = proxy_str.strip()
    scheme = "http"
    if "://" in value:
        scheme, value = value.split("://", 1)
    user = password = host = port = None
    if "@" in value:
        try:
            left, right = value.split("@")
        except ValueError:
            raise LegacyInputError() from None
        left_parts, right_parts = left.split(":"), right.split(":")
        if len(right_parts) >= 2 and right_parts[-1].isdigit():
            user = left_parts[0]
            password = ":".join(left_parts[1:]) if len(left_parts) > 1 else None
            host, port = ":".join(right_parts[:-1]), right_parts[-1]
        elif len(left_parts) >= 2 and left_parts[-1].isdigit():
            host, port = ":".join(left_parts[:-1]), left_parts[-1]
            user = right_parts[0]
            password = ":".join(right_parts[1:]) if len(right_parts) > 1 else None
        else:
            user = left_parts[0]
            password = ":".join(left_parts[1:]) if len(left_parts) > 1 else None
            host = ":".join(right_parts[:-1]) if len(right_parts) > 1 else right_parts[0]
            port = right_parts[-1] if len(right_parts) > 1 else None
    else:
        parts = value.split(":")
        if len(parts) == 2:
            host, port = parts
        elif len(parts) == 4:
            host, port, user, password = parts
        elif len(parts) > 4:
            host, port, user, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
        else:
            host = parts[0]
            port = parts[1] if len(parts) > 1 else None
    if not host or not port:
        return None
    if user and password:
        url = f"{scheme}://{user}:{password}@{host}:{port}"
    elif user:
        url = f"{scheme}://{user}@{host}:{port}"
    else:
        url = f"{scheme}://{host}:{port}"
    return LegacyProxy({"http": url, "https": url})


class LegacyOperation(StrEnum):
    RECEIPT_PHOTO = "receipt_photo"
    RECEIPT_TEXT = "receipt_text"
    EDIT_CAPTION = "edit_caption"
    EDIT_TEXT = "edit_text"
    LEGACY_PHOTO = "legacy_photo"
    LEGACY_TEXT = "legacy_text"
    BOT_TEXT = "bot_text"
    BOT_PHOTO_BYTES = "bot_photo_bytes"


@dataclass(frozen=True)
class PathPhotoPart:
    path: str = field(repr=False)
    field_name: Literal["photo"] = "photo"
    filename: Literal["photo"] = "photo"
    content_type: Literal["image/png"] = "image/png"


@dataclass(frozen=True)
class BytesPhotoPart:
    value: bytes = field(repr=False)
    field_name: Literal["photo"] = "photo"
    filename: None = None
    content_type: None = None


@dataclass(frozen=True)
class LegacyRequestPlan:
    operation: LegacyOperation
    method: str
    fields: Mapping[str, object] = field(repr=False)
    files: tuple[PathPhotoPart | BytesPhotoPart, ...] = field(repr=False)
    timeout: int
    verify_tls: Literal[False] = False
    encoding: Literal["form"] = "form"

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


def build_legacy_request(
    operation: LegacyOperation,
    *,
    chat_id: int,
    message: str,
    thread_id: str | None = None,
    message_id: int | None = None,
    images: Sequence[str] = (),
    photo_bytes: bytes = b"",
    reply_to_message_id: int | None = None,
) -> LegacyRequestPlan:
    """Token-free request data. Defaults here are new API conveniences only."""
    _string(message)
    if not isinstance(operation, LegacyOperation) or type(chat_id) is not int:
        raise LegacyInputError()
    if thread_id is not None:
        _string(thread_id)
    if message_id is not None and type(message_id) is not int:
        raise LegacyInputError()
    if reply_to_message_id is not None and type(reply_to_message_id) is not int:
        raise LegacyInputError()
    if isinstance(images, (str, bytes)) or not isinstance(images, Sequence):
        raise LegacyInputError()
    for path in images:
        _string(path)
    if type(photo_bytes) is not bytes:
        raise LegacyInputError()
    fields: dict[str, object] = {"chat_id": chat_id}
    files: tuple[PathPhotoPart | BytesPhotoPart, ...] = ()
    if operation in {LegacyOperation.EDIT_CAPTION, LegacyOperation.EDIT_TEXT}:
        if message_id is None:
            raise LegacyInputError()
        fields["message_id"] = message_id
        caption = operation == LegacyOperation.EDIT_CAPTION
        method = "editMessageCaption" if caption else "editMessageText"
        fields["caption" if caption else "text"] = message
        timeout = 20
    elif operation in {
        LegacyOperation.RECEIPT_PHOTO,
        LegacyOperation.LEGACY_PHOTO,
        LegacyOperation.BOT_PHOTO_BYTES,
    }:
        method = "sendPhoto"
        fields["caption"] = message
        if operation == LegacyOperation.BOT_PHOTO_BYTES:
            timeout = 30
            files = (BytesPhotoPart(photo_bytes),)
            if reply_to_message_id:
                fields["reply_to_message_id"] = reply_to_message_id
        else:
            timeout = 40
            files = tuple(PathPhotoPart(path) for path in images)
    else:
        method = "sendMessage"
        fields["text"] = message
        timeout = {
            LegacyOperation.RECEIPT_TEXT: 20,
            LegacyOperation.LEGACY_TEXT: 15,
            LegacyOperation.BOT_TEXT: 30,
        }[operation]
    if operation not in {LegacyOperation.BOT_TEXT, LegacyOperation.BOT_PHOTO_BYTES}:
        fields["parse_mode"] = "HTML"
        if operation not in {LegacyOperation.EDIT_CAPTION, LegacyOperation.EDIT_TEXT} and thread_id:
            fields["message_thread_id"] = thread_id
    return LegacyRequestPlan(operation, method, fields, files, timeout)


@dataclass(frozen=True)
class LegacyResponse:
    value: Any = field(repr=False)
    exception_propagates: bool = False
    caught_exception: bool = False
    logging: Literal["none", "console", "console_and_error_file"] = "none"


def evaluate_legacy_response(
    operation: LegacyOperation, decoded: Any = None, *, ordinary_exception: bool = False
) -> LegacyResponse:
    """Characterize JSON/result handling; no actual exception or logging is used."""
    if not isinstance(operation, LegacyOperation):
        raise LegacyInputError()
    receipt = operation in {LegacyOperation.RECEIPT_PHOTO, LegacyOperation.RECEIPT_TEXT}
    edit = operation in {LegacyOperation.EDIT_CAPTION, LegacyOperation.EDIT_TEXT}
    if not ordinary_exception:
        try:
            if receipt:
                return LegacyResponse(
                    decoded["result"]["message_id"] if decoded.get("ok") else None
                )
            if edit:
                return LegacyResponse(bool(decoded.get("ok")))
            if operation == LegacyOperation.LEGACY_PHOTO:
                return LegacyResponse(True)
            if operation == LegacyOperation.LEGACY_TEXT:
                return LegacyResponse(None)
            return LegacyResponse(decoded)
        except Exception:
            ordinary_exception = True
    if receipt or edit:
        return LegacyResponse(None if receipt else False, caught_exception=True, logging="console")
    if operation == LegacyOperation.LEGACY_PHOTO:
        return LegacyResponse(False, caught_exception=True, logging="console_and_error_file")
    return LegacyResponse(None, exception_propagates=True)


def selector_first_kind(images: object) -> Literal["photo", "text"]:
    return "text" if images is None else "photo"


def selector_text_fallback(photo_result: object) -> bool:
    return not photo_result


def receipt_first_kind(images: object) -> Literal["photo", "text"]:
    return "photo" if images else "text"


def receipt_text_fallback(photo_result: object) -> bool:
    return photo_result is None


@dataclass(frozen=True)
class LegacyReceipt:
    chat_id: int = field(repr=False)
    message_id: Any = field(repr=False)
    thread_id: str | None = field(repr=False)
    kind: Literal["photo", "text"]


def accepted_legacy_receipt(
    destination: LegacyDestination, message_id: Any, kind: Literal["photo", "text"]
) -> LegacyReceipt | None:
    if message_id is None:
        return None
    return LegacyReceipt(destination.chat_id, message_id, destination.thread_id, kind)


def producer_repeats_fanout(fanout_result: object) -> bool:
    """Static falsey-return mismatch; not evidence that the first send failed."""
    return not fanout_result


@dataclass(frozen=True)
class NotificationState:
    last_message: str = field(repr=False)
    last_time: float


@dataclass(frozen=True)
class InactivityDecision:
    suppressed: bool
    state_before_delivery: NotificationState


def inactivity_decision(
    state: NotificationState, message: str, *, repeating: object, now: float
) -> InactivityDecision:
    _string(state.last_message)
    _string(message)
    if not repeating and INACTIVITY_MARKER in state.last_message and INACTIVITY_MARKER in message:
        return InactivityDecision(True, state)
    return InactivityDecision(False, NotificationState(message, now))


@dataclass(frozen=True)
class ImportantDecision:
    expanded_filter: tuple[str, ...] = field(repr=False)
    matched: bool


def important_decision(filters: Sequence[str], message: str) -> ImportantDecision:
    """Return the legacy append as data, without mutating caller-owned input."""
    _string(message)
    if isinstance(filters, (str, bytes)) or not isinstance(filters, Sequence):
        raise LegacyInputError()
    for item in filters:
        _string(item)
    expanded = tuple(filters)
    if OLD_INACTIVITY_FILTER in expanded and INACTIVITY_MARKER not in expanded:
        expanded += (INACTIVITY_MARKER,)
    return ImportantDecision(expanded, any(item in message for item in expanded))


@dataclass(frozen=True)
class ItemLine:
    name: str = field(repr=False)
    price: str = field(repr=False)


def format_item_message(old_message: str, items: Iterable[ItemLine]) -> str:
    """Raw HTML is proven behavior. No price lookup, icon work or prefix inference."""
    _string(old_message)
    message = old_message
    first = True
    for item in items:
        _string(item.name)
        _string(item.price)
        if first:
            message += "\n\n"
            first = False
        message += f"{item.price} | {item.name}\n"
    return message
