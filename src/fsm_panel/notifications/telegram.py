"""Owned, single-attempt Telegram HTTP transport with explicit uncertain outcomes."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import stat
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from .models import (
    ConfigurationCode,
    DeliveryCode,
    DeliveryResult,
    DeliveryState,
    RequestTimeouts,
    TelegramCancelled,
    TelegramConfigurationError,
)

_ORIGIN = "https://api.telegram.org"
_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{1,256}\Z")
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_PRIVATE_REQUEST: ContextVar[bool] = ContextVar("telegram_private_request", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_REQUEST.get()


def _install_log_filters() -> None:
    # These are the logger names in pinned httpx 0.28.1 / httpcore 1.0.9.
    # A ContextVar leaves unrelated requests and other tasks' logs untouched.
    for name in (
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _PrivateRequestFilter) for item in logger.filters):
            logger.addFilter(_PrivateRequestFilter())


@dataclass(frozen=True)
class ProxySettings:
    """Validated conventional HTTP(S) proxy URL; never use the legacy parser here."""

    url: str = field(repr=False)

    def __post_init__(self) -> None:
        value = self.url
        if (
            type(value) is not str
            or not 1 <= len(value) <= 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            raise TelegramConfigurationError(ConfigurationCode.INVALID_PROXY)
        try:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"}:
                raise TelegramConfigurationError(ConfigurationCode.UNSUPPORTED_PROXY)
            if (
                not parts.hostname
                or parts.port is None
                or not 1 <= parts.port <= 65535
                or parts.path not in {"", "/"}
                or parts.query
                or parts.fragment
                or parts.netloc.count("@") > 1
            ):
                raise ValueError
            host = parts.hostname
            try:
                ipaddress.ip_address(host)
            except ValueError:
                labels = host.rstrip(".").split(".")
                if len(host) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in labels
                ):
                    raise ValueError from None
            if parts.username is not None:
                if not parts.username:
                    raise ValueError
                for credential in (parts.username, parts.password or ""):
                    decoded = unquote(credential, errors="strict")
                    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
                        raise ValueError
        except TelegramConfigurationError:
            raise
        except ValueError, UnicodeError:
            raise TelegramConfigurationError(ConfigurationCode.INVALID_PROXY) from None


@dataclass
class _Operation:
    method: str
    fields: dict[str, str] = field(repr=False)
    paths: tuple[Path, ...] = field(default=(), repr=False)
    photo_bytes: bytes | None = field(default=None, repr=False)
    edit_id: int | None = None
    dispatched: bool = False
    result: DeliveryResult | None = None


def _not_sent(code: DeliveryCode) -> DeliveryResult:
    return DeliveryResult(DeliveryState.NOT_SENT, code)


def _uncertain(operation: _Operation, code: DeliveryCode) -> DeliveryResult:
    return DeliveryResult(
        DeliveryState.UNKNOWN_OUTCOME if operation.dispatched else DeliveryState.NOT_SENT, code
    )


def _positive_id(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def _topic(value: object) -> bool:
    return _positive_id(value) or (
        type(value) is str
        and re.fullmatch(r"[0-9]{1,20}", value) is not None
        and 0 < int(value) < 2**63
    )


def _content(value: object, *, empty: bool) -> bool:
    if type(value) is not str or len(value) > 65536 or (not empty and not value) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _fields(chat_id: int, message: str, *, caption: bool) -> dict[str, str] | None:
    if (
        type(chat_id) is not int
        or chat_id == 0
        or not -(2**63) < chat_id < 2**63
        or not _content(message, empty=caption)
    ):
        return None
    return {"chat_id": str(chat_id), "caption" if caption else "text": message}


def _response_result(status: int, content: bytes, edit_id: int | None) -> DeliveryResult:
    unknown = DeliveryResult(
        DeliveryState.UNKNOWN_OUTCOME, DeliveryCode.INVALID_RESPONSE, http_status=status
    )
    if 300 <= status < 400:
        return DeliveryResult(
            DeliveryState.UNKNOWN_OUTCOME, DeliveryCode.HTTP_STATUS, http_status=status
        )
    try:
        data = json.loads(content)
    except ValueError, UnicodeError, RecursionError:
        return unknown
    if type(data) is not dict:
        return unknown
    if data.get("ok") is False:
        error = data.get("error_code")
        return DeliveryResult(
            DeliveryState.REJECTED,
            DeliveryCode.REMOTE_REJECTED,
            http_status=status,
            error_code=error if type(error) is int and 0 < error <= 9999 else None,
        )
    if not 200 <= status < 300:
        return DeliveryResult(
            DeliveryState.UNKNOWN_OUTCOME, DeliveryCode.HTTP_STATUS, http_status=status
        )
    if data.get("ok") is not True or type(data.get("result")) is not dict:
        return unknown
    message_id = data["result"].get("message_id")
    if not _positive_id(message_id) or (edit_id is not None and message_id != edit_id):
        return unknown
    return DeliveryResult(DeliveryState.SENT, DeliveryCode.OK, message_id, status)


async def _wait_cleanup(task: asyncio.Task[Any]) -> bool:
    """Join owned cleanup even if its waiter is repeatedly cancelled."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    return cancelled


class TelegramClient:
    """One attempt per method call, on one event loop, with an owned HTTP client.

    ``transport`` is exclusively an offline MockTransport seam. A configured
    proxy cannot be combined with it. No supplied AsyncClient, base URL, event
    hooks, redirect setting, retry policy or TLS override can bypass the policy.
    """

    def __init__(
        self,
        token: str,
        *,
        proxy: ProxySettings | None = None,
        timeouts: RequestTimeouts | None = None,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        if type(token) is not str or _TOKEN.fullmatch(token) is None:
            raise TelegramConfigurationError(ConfigurationCode.INVALID_TOKEN)
        if proxy is not None and not isinstance(proxy, ProxySettings):
            raise TelegramConfigurationError(ConfigurationCode.INVALID_PROXY)
        if timeouts is not None and not isinstance(timeouts, RequestTimeouts):
            raise TelegramConfigurationError(ConfigurationCode.INVALID_TIMEOUT)
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TelegramConfigurationError(ConfigurationCode.INVALID_TRANSPORT)
        if transport is not None and proxy is not None:
            raise TelegramConfigurationError(ConfigurationCode.PROXY_WITH_MOCK_TRANSPORT)
        self._token = token
        self._timeouts = timeouts or RequestTimeouts()
        self._tasks: set[asyncio.Task[DeliveryResult]] = set()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        _install_log_filters()
        private = _PRIVATE_REQUEST.set(True)
        try:
            network = (
                transport
                if transport is not None
                else httpx.AsyncHTTPTransport(
                    verify=True,
                    trust_env=False,
                    retries=0,
                    proxy=proxy.url if proxy is not None else None,
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                )
            )
            self._http = httpx.AsyncClient(
                transport=network,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(
                    connect=self._timeouts.connect,
                    read=self._timeouts.read,
                    write=self._timeouts.write,
                    pool=self._timeouts.pool,
                ),
            )
        except Exception:
            raise TelegramConfigurationError(ConfigurationCode.CLIENT_SETUP_FAILED) from None
        finally:
            _PRIVATE_REQUEST.reset(private)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def __aenter__(self) -> TelegramClient:
        if self._closing:
            raise TelegramConfigurationError(ConfigurationCode.CLIENT_CLOSE_FAILED)
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def send_text(
        self, chat_id: int, text: str, *, thread_id: int | str | None = None, html: bool = True
    ) -> DeliveryResult:
        fields = _fields(chat_id, text, caption=False)
        if (
            fields is None
            or type(html) is not bool
            or (thread_id is not None and (not html or not _topic(thread_id)))
        ):
            return _not_sent(DeliveryCode.INVALID_INPUT)
        if html:
            fields["parse_mode"] = "HTML"
        if thread_id is not None:
            fields["message_thread_id"] = str(thread_id)
        return await self._submit(_Operation("sendMessage", fields))

    async def send_photo_paths(
        self,
        chat_id: int,
        caption: str,
        images: list[str | Path] | tuple[str | Path, ...],
        *,
        thread_id: int | str | None = None,
    ) -> DeliveryResult:
        fields = _fields(chat_id, caption, caption=True)
        if (
            fields is None
            or (thread_id is not None and not _topic(thread_id))
            or type(images) not in (list, tuple)
            or not 1 <= len(images) <= 10
            or not all(
                (type(path) is str and bool(path)) or isinstance(path, Path) for path in images
            )
        ):
            return _not_sent(DeliveryCode.INVALID_INPUT)
        fields["parse_mode"] = "HTML"
        if thread_id is not None:
            fields["message_thread_id"] = str(thread_id)
        return await self._submit(
            _Operation("sendPhoto", fields, tuple(Path(path) for path in images))
        )

    async def send_photo_bytes(
        self,
        chat_id: int,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> DeliveryResult:
        fields = _fields(chat_id, caption, caption=True)
        if (
            fields is None
            or type(photo_bytes) is not bytes
            or not 1 <= len(photo_bytes) <= _MAX_PHOTO_BYTES
            or (reply_to_message_id is not None and not _positive_id(reply_to_message_id))
        ):
            return _not_sent(DeliveryCode.INVALID_INPUT)
        if reply_to_message_id is not None:
            fields["reply_to_message_id"] = str(reply_to_message_id)
        return await self._submit(_Operation("sendPhoto", fields, photo_bytes=photo_bytes))

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> DeliveryResult:
        return await self._edit("editMessageText", chat_id, message_id, text, caption=False)

    async def edit_caption(self, chat_id: int, message_id: int, caption: str) -> DeliveryResult:
        return await self._edit("editMessageCaption", chat_id, message_id, caption, caption=True)

    async def _edit(
        self, method: str, chat_id: int, message_id: int, message: str, *, caption: bool
    ) -> DeliveryResult:
        fields = _fields(chat_id, message, caption=caption)
        if fields is None or not _positive_id(message_id):
            return _not_sent(DeliveryCode.INVALID_INPUT)
        fields.update(message_id=str(message_id), parse_mode="HTML")
        return await self._submit(_Operation(method, fields, edit_id=message_id))

    async def _submit(self, operation: _Operation) -> DeliveryResult:
        if self._closing:
            return _not_sent(DeliveryCode.CLIENT_CLOSED)
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise TelegramCancelled(_not_sent(DeliveryCode.CANCELLED)) from None
        task = asyncio.create_task(self._execute(operation), name="telegram-request")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            return await task
        except asyncio.CancelledError:
            result = operation.result or _uncertain(operation, DeliveryCode.CANCELLED)
            raise TelegramCancelled(result) from None

    async def _execute(self, operation: _Operation) -> DeliveryResult:
        private = _PRIVATE_REQUEST.set(True)
        stack = ExitStack()
        response: httpx.Response | None = None
        cancelled = False
        try:
            # Exposes a before-dispatch cancellation checkpoint.
            await asyncio.sleep(0)
            files: Any = None
            if operation.paths:
                files = []
                for path in operation.paths:
                    handle = stack.enter_context(path.open("rb"))
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= _MAX_PHOTO_BYTES:
                        operation.result = _not_sent(DeliveryCode.LOCAL_FILE_ERROR)
                        return operation.result
                    files.append(("photo", ("photo", handle, "image/png")))
            elif operation.photo_bytes is not None:
                # Direct bytes, without an explicit filename/MIME tuple, as observed.
                files = {"photo": operation.photo_bytes}
            async with asyncio.timeout(self._timeouts.total):
                request = self._http.build_request(
                    "POST",
                    f"{_ORIGIN}/bot{self._token}/{operation.method}",
                    data=operation.fields,
                    files=files,
                    headers={"Accept-Encoding": "identity"},
                )
                operation.dispatched = True
                response = await self._http.send(request, stream=True, follow_redirects=False)
                content = bytearray()
                # Read the owned stream directly: aiter_bytes() auto-closes the
                # response inside the cancellable read. Our close is shielded.
                assert isinstance(response.stream, httpx.AsyncByteStream)
                async for chunk in response.stream:
                    if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                        operation.result = DeliveryResult(
                            DeliveryState.UNKNOWN_OUTCOME,
                            DeliveryCode.RESPONSE_TOO_LARGE,
                            http_status=response.status_code,
                        )
                        break
                    content.extend(chunk)
                else:
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        operation.result = DeliveryResult(
                            DeliveryState.UNKNOWN_OUTCOME,
                            DeliveryCode.INVALID_RESPONSE,
                            http_status=response.status_code,
                        )
                    else:
                        operation.result = _response_result(
                            response.status_code, bytes(content), operation.edit_id
                        )
        except asyncio.CancelledError:
            cancelled = True
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout:
            operation.result = (
                _not_sent(DeliveryCode.CONNECT_FAILED)
                if response is None
                else _uncertain(operation, DeliveryCode.TRANSPORT_ERROR)
            )
        except TimeoutError, httpx.TimeoutException:
            operation.result = _uncertain(operation, DeliveryCode.TIMEOUT)
        except httpx.HTTPError:
            operation.result = _uncertain(operation, DeliveryCode.TRANSPORT_ERROR)
        except OSError, ValueError:
            operation.result = _uncertain(
                operation,
                DeliveryCode.TRANSPORT_ERROR
                if operation.dispatched
                else DeliveryCode.LOCAL_FILE_ERROR,
            )
        except Exception:
            operation.result = _uncertain(operation, DeliveryCode.INTERNAL_ERROR)
        finally:
            if response is not None:
                cleanup = asyncio.create_task(response.aclose(), name="telegram-response-close")
                cancelled = await _wait_cleanup(cleanup) or cancelled
                try:
                    cleanup.result()
                except Exception, asyncio.CancelledError:
                    if operation.result is None:
                        operation.result = _uncertain(operation, DeliveryCode.TRANSPORT_ERROR)
            try:
                stack.close()
            except Exception:
                if operation.result is None:
                    operation.result = _uncertain(operation, DeliveryCode.LOCAL_FILE_ERROR)
            _PRIVATE_REQUEST.reset(private)
        if cancelled:
            operation.result = operation.result or _uncertain(operation, DeliveryCode.CANCELLED)
            raise TelegramCancelled(operation.result) from None
        if operation.result is None:
            operation.result = _uncertain(operation, DeliveryCode.INTERNAL_ERROR)
        return operation.result

    async def _close(self) -> None:
        private = _PRIVATE_REQUEST.set(True)
        try:
            pending = tuple(self._tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self._http.aclose()
            self._closed = True
        except Exception:
            raise TelegramConfigurationError(ConfigurationCode.CLIENT_CLOSE_FAILED) from None
        finally:
            _PRIVATE_REQUEST.reset(private)

    async def aclose(self) -> None:
        """Cancel and join requests, then close HTTPX; repeated calls are safe."""
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close(), name="telegram-client-close")
        cancelled = await _wait_cleanup(self._close_task)
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError() from None
