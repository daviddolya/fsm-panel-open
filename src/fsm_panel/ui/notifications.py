"""Explicit manual notifications, with no real network in simulation mode."""

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fsm_panel.core.models import Admission
from fsm_panel.notifications import (
    DeliveryCode,
    DeliveryResult,
    DeliveryState,
    ProxySettings,
    TelegramCancelled,
    TelegramClient,
    TelegramConfigurationError,
)
from fsm_panel.notifications.legacy import LegacyInputError, parse_receipt_destination

from .item_composite import ItemCompositePanel


@dataclass
class _Job:
    action: str
    token: str = field(repr=False)
    destination: str = field(repr=False)
    message: str = field(repr=False)
    images: tuple[str, ...] = field(repr=False)
    message_id: int
    proxy: str = field(repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)


class NotificationPanel(QWidget):
    closing_started = Signal()

    def __init__(self, owner, *, client_factory=TelegramClient) -> None:
        super().__init__()
        self.owner = owner
        self._client_factory = client_factory
        self._future: concurrent.futures.Future | None = None
        self._job: _Job | None = None
        self._request = 0
        self._closing = False
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs)
        manual = QWidget()
        tabs.addTab(manual, "Telegram")
        self.composite = ItemCompositePanel(owner)
        tabs.addTab(self.composite, "Два изображения → PNG")
        layout = QVBoxLayout(manual)
        self.notice = QLabel(
            "Ручные уведомления Telegram. Для отправки нужен действующий допуск. "
            "Симулятор не отправляет сообщения. Автоматического повтора нет; при неизвестном "
            "результате проверьте чат перед новой отправкой."
        )
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.load_button = QPushButton("Загрузить токен и адрес из настроек")
        self.load_button.clicked.connect(self._load_settings)
        layout.addWidget(self.load_button)
        form = QFormLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("Один chat_id или chat_id:thread_id")
        self.proxy = QLineEdit()
        self.proxy.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy.setPlaceholderText("Необязательно: http(s)://host:port")
        self.action = QComboBox()
        for label, value in (
            ("Отправить текст", "text"),
            ("Отправить фото с подписью", "photo"),
            ("Изменить текст сообщения", "edit_text"),
            ("Изменить подпись фото", "edit_caption"),
        ):
            self.action.addItem(label, value)
        self.message_id = QLineEdit()
        self.message_id.setPlaceholderText("Для редактирования")
        form.addRow("Токен бота", self.token)
        form.addRow("Получатель", self.destination)
        form.addRow("Прокси", self.proxy)
        form.addRow("Действие", self.action)
        form.addRow("ID сообщения", self.message_id)
        layout.addLayout(form)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("Текст или подпись; поддерживается разметка HTML")
        self.message.setMaximumHeight(110)
        layout.addWidget(self.message)
        photo_row = QHBoxLayout()
        self.photo_path = QLineEdit()
        self.photo_path.setPlaceholderText("Файл изображения")
        self.browse_button = QPushButton("Выбрать фото…")
        self.browse_button.clicked.connect(self._browse)
        photo_row.addWidget(self.photo_path)
        photo_row.addWidget(self.browse_button)
        layout.addLayout(photo_row)
        actions = QHBoxLayout()
        self.send_button = QPushButton("Выполнить")
        self.cancel_button = QPushButton("Отменить ожидание")
        self.send_button.clicked.connect(self._submit)
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.send_button)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.status = QLabel("Выберите действие и заполните получателя.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        owner.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        owner.bridge.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.closing_started.connect(self._refresh, Qt.ConnectionType.QueuedConnection)
        self._refresh()

    def _available(self) -> bool:
        return not self._closing and not self.owner._closing and self._future is None

    @Slot()
    def _refresh(self) -> None:
        available = self._available()
        for control in (
            self.load_button,
            self.token,
            self.destination,
            self.proxy,
            self.action,
            self.message_id,
            self.message,
            self.photo_path,
            self.browse_button,
        ):
            control.setEnabled(available)
        self.send_button.setEnabled(available and not self.owner.simulate)
        self.cancel_button.setEnabled(
            self._future is not None and self._job is not None and not self._job.cancelled.is_set()
        )

    def _load_settings(self) -> None:
        if not self._available():
            return
        for key, widget in (("TG_BOT_API_KEY", self.token), ("TG_BOT_CHAT_ID", self.destination)):
            value = self.owner.settings.get(key)
            widget.setText(value if type(value) is str else "")
        proxy = self.owner.settings.get("PROXY_FOR_TG")
        self.proxy.setText(
            proxy if type(proxy) is str else "" if proxy is None else "[invalid proxy value]"
        )
        self.status.setText(
            "Настройки загружены. Выберите одного получателя; изменения здесь не сохраняются."
        )

    def _browse(self) -> None:
        if self._available():
            path, _ = QFileDialog.getOpenFileName(
                self, "Изображение", "", "Images (*.png *.jpg *.jpeg)"
            )
            if path:
                self.photo_path.setText(path)

    def _submit(self) -> None:
        if not self._available() or self.owner.simulate:
            return
        action = self.action.currentData()
        try:
            message_id = int(self.message_id.text()) if action.startswith("edit_") else 0
        except ValueError:
            self.status.setText("Укажите целый ID сообщения.")
            return
        job = _Job(
            action,
            self.token.text(),
            self.destination.text(),
            self.message.toPlainText(),
            (self.photo_path.text(),),
            message_id,
            self.proxy.text(),
        )
        self._request += 1
        self._job = job
        self.status.setText("Проверка допуска и выполнение запроса…")
        try:
            self._future = self.owner.bridge.submit(self._perform(job, self._request))
        except RuntimeError:
            self._closing = True
            self._job = None
            self.status.setText("Обработчик закрыт.")
        self._refresh()

    async def _perform(self, job: _Job, request: int) -> dict:
        job.task = asyncio.current_task()
        client = None
        started = False
        result = DeliveryResult(DeliveryState.NOT_SENT, DeliveryCode.CANCELLED)
        status = None
        try:
            if not job.cancelled.is_set():
                admission = await asyncio.wait_for(
                    self.owner.coordinator.lease.admit("notifications"), 10
                )
                if not isinstance(admission, Admission) or admission.allowed is not True:
                    status = "license_denied"
                elif not job.cancelled.is_set():
                    destination = parse_receipt_destination(job.destination)
                    proxy = ProxySettings(job.proxy) if job.proxy else None
                    client = self._client_factory(job.token, proxy=proxy)
                    if not job.cancelled.is_set():
                        started = True
                        if job.action == "text":
                            result = await client.send_text(
                                destination.chat_id, job.message, thread_id=destination.thread_id
                            )
                        elif job.action == "photo":
                            result = await client.send_photo_paths(
                                destination.chat_id,
                                job.message,
                                job.images,
                                thread_id=destination.thread_id,
                            )
                        elif job.action == "edit_text":
                            result = await client.edit_text(
                                destination.chat_id, job.message_id, job.message
                            )
                        elif job.action == "edit_caption":
                            result = await client.edit_caption(
                                destination.chat_id, job.message_id, job.message
                            )
                        else:
                            result = DeliveryResult(
                                DeliveryState.NOT_SENT, DeliveryCode.INVALID_INPUT
                            )
        except TelegramCancelled as error:
            result = error.result
        except asyncio.CancelledError:
            result = DeliveryResult(
                DeliveryState.UNKNOWN_OUTCOME if started else DeliveryState.NOT_SENT,
                DeliveryCode.CANCELLED,
            )
        except LegacyInputError, TelegramConfigurationError:
            result = DeliveryResult(DeliveryState.NOT_SENT, DeliveryCode.INVALID_INPUT)
        except Exception:
            result = DeliveryResult(
                DeliveryState.UNKNOWN_OUTCOME if started else DeliveryState.NOT_SENT,
                DeliveryCode.INTERNAL_ERROR,
            )
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except asyncio.CancelledError:
                    # TelegramClient joins its close task before propagating
                    # cancellation. Preserve an already confirmed result.
                    if not client.is_closed:
                        status = "cleanup_failed"
                except Exception:
                    status = "cleanup_failed"
            job.task = None
        return {
            "notification_panel": id(self),
            "request": request,
            "result": result,
            "status": status,
        }

    @Slot(object)
    def _completed(self, response: object) -> None:
        if (
            not isinstance(response, dict)
            or response.get("notification_panel") != id(self)
            or response.get("request") != self._request
        ):
            return
        self._future = None
        self._job = None
        result = response["result"]
        if response["status"] == "license_denied":
            message = "Отправка не началась: нужен действующий допуск."
        else:
            message = {
                DeliveryState.NOT_SENT: "Сообщение не отправлено.",
                DeliveryState.SENT: "Запрос подтверждён Telegram.",
                DeliveryState.REJECTED: "Telegram отклонил запрос.",
                DeliveryState.UNKNOWN_OUTCOME: "Результат неизвестен. Проверьте чат перед новой отправкой.",
            }[result.state]
            message += f" Код: {result.code.value}."
            if result.message_id is not None:
                message += f" ID сообщения: {result.message_id}."
        if response["status"] == "cleanup_failed":
            message += " Ошибка закрытия соединения; перезапустите панель."
            self._closing = True
        self.status.setText(message)
        self._refresh()

    @Slot(str)
    def _failed(self, code: str) -> None:
        pending = self._future
        if (
            pending is not None
            and pending.done()
            and (pending.cancelled() or pending.exception() is not None)
        ):
            self._future = None
            self._job = None
            self._closing = True
            self.status.setText(
                "Операция прервана. Результат неизвестен; проверьте чат и перезапустите панель."
            )
            self._refresh()

    def _cancel_job(self) -> None:
        job = self._job
        if job is not None and not job.cancelled.is_set():
            job.cancelled.set()

            def cancel() -> None:
                if job.task is not None:
                    job.task.cancel()

            self.owner.bridge.loop.call_soon_threadsafe(cancel)

    def _cancel(self) -> None:
        self._cancel_job()
        self.status.setText("Отмена ожидания и закрытие соединения…")
        self._refresh()

    async def aclose(self) -> None:
        self._closing = True
        self.closing_started.emit()
        self._cancel_job()
        tasks: list[asyncio.Future] = [asyncio.ensure_future(self.composite.aclose())]
        if self._future is not None:
            tasks.append(asyncio.wrap_future(self._future))
        pending = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        for result in pending.result():
            if isinstance(result, BaseException):
                raise result
        if cancelled:
            raise asyncio.CancelledError()
