"""Public notification results. No response bodies, URLs or exception strings."""

import asyncio
import math
from dataclasses import dataclass
from enum import StrEnum


class DeliveryState(StrEnum):
    NOT_SENT = "not_sent"
    SENT = "sent"
    REJECTED = "rejected"
    UNKNOWN_OUTCOME = "unknown_outcome"


class DeliveryCode(StrEnum):
    OK = "ok"
    INVALID_INPUT = "invalid_input"
    LOCAL_FILE_ERROR = "local_file_error"
    CLIENT_CLOSED = "client_closed"
    CONNECT_FAILED = "connect_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    HTTP_STATUS = "http_status"
    REMOTE_REJECTED = "remote_rejected"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class DeliveryResult:
    state: DeliveryState
    code: DeliveryCode
    message_id: int | None = None
    http_status: int | None = None
    error_code: int | None = None


class TelegramCancelled(asyncio.CancelledError):
    """Cancellation still propagates; the caller can retain the delivery outcome."""

    def __init__(self, result: DeliveryResult) -> None:
        self.result = result
        super().__init__(DeliveryCode.CANCELLED.value)


class ConfigurationCode(StrEnum):
    INVALID_TOKEN = "invalid_token"
    INVALID_PROXY = "invalid_proxy"
    UNSUPPORTED_PROXY = "unsupported_proxy"
    INVALID_TIMEOUT = "invalid_timeout"
    INVALID_TRANSPORT = "invalid_transport"
    PROXY_WITH_MOCK_TRANSPORT = "proxy_with_mock_transport"
    CLIENT_SETUP_FAILED = "client_setup_failed"
    CLIENT_CLOSE_FAILED = "client_close_failed"


class TelegramConfigurationError(ValueError):
    def __init__(self, code: ConfigurationCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class RequestTimeouts:
    """Rebuild limits, in seconds; total is a separate asyncio deadline."""

    connect: float = 5.0
    read: float = 40.0
    write: float = 40.0
    pool: float = 5.0
    total: float = 60.0

    def __post_init__(self) -> None:
        for value in (self.connect, self.read, self.write, self.pool, self.total):
            if type(value) not in (float, int) or not 0 < value <= 120 or not math.isfinite(value):
                raise TelegramConfigurationError(ConfigurationCode.INVALID_TIMEOUT)
