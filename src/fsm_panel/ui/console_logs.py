"""Read-only analysis of an explicitly selected local console-log copy."""

import asyncio
import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fsm_panel.adapters.console_parser import (
    ConsoleParserContext,
    ConsoleParseResult,
    read_console_log,
)


@dataclass
class _ReadJob:
    path: str = field(repr=False)
    context: ConsoleParserContext = field(repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    finished: concurrent.futures.Future = field(
        default_factory=concurrent.futures.Future, repr=False
    )


def _summary(result: ConsoleParseResult) -> str:
    """Bound the display even when a recovered marker contains a huge string."""
    identity = {
        "cached": "из введённого кэша",
        "fallback": "резервное значение 1; аккаунт не установлен",
        "auth": "из строки AuthStatus",
        "file_error": "нет: чтение завершилось ошибкой",
    }[result.identity_origin]
    lines = [
        f"Источник идентификатора: {identity}. Подлинность не проверена.",
        f"Прочитано: {result.read.bytes_read} байт; начало: {result.read.offset}; "
        f"размер файла: {result.read.source_size} байт.",
        f"Ошибки строк: {result.line_errors}; отсутствующие даты: {result.missing_timestamps}.",
        f"Предложения изменения кэша/relay: {len(result.proposals)}. Они не применяются.",
        "",
    ]
    labels = (
        "Идентификатор",
        "Последний сервер",
        "Вход в партию",
        "Проверка ping",
        "Получение билета",
        "Меню после матча",
        "Начало загрузки",
        "Загрузка меню",
    )
    for label, value in zip(labels, result.observation.as_legacy_tuple(), strict=True):
        if isinstance(value, str):
            # repr keeps control/newline characters from masquerading as other fields.
            display = repr(value[:256]) + (" … (сокращено)" if len(value) > 256 else "")
        else:
            display = str(value)
        lines.append(f"{label}: {display}")
    lines.extend(("", "Время — Unix timestamp, вычисленное в локальном часовом поясе."))
    if result.issues:
        lines.append("Диагностика: " + ", ".join(result.issues))
    return "\n".join(lines)


class ConsoleLogPanel(QWidget):
    operation_finished = Signal(object)
    closing_started = Signal()

    def __init__(self, owner, *, reader=read_console_log) -> None:
        super().__init__()
        self.owner = owner
        self._reader = reader
        self._job: _ReadJob | None = None
        self._future: concurrent.futures.Future | None = None
        self._request = 0
        self._closing = False
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Выберите локальную копию консольного лога. Чтение не изменяет файл и "
            "не запускает игровые действия. Анализируется конец файла: до 12 000 000 байт, "
            "при заданном кэше — до 2 000 000 байт; максимум 200 000 строк. "
            "Год берётся из поля ниже, часовой пояс — с этого компьютера. Допуск не нужен."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.browse_button = QPushButton("Выбрать…")
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.path)
        row.addWidget(self.browse_button)
        form.addRow("Копия лога", row)
        self.year = QSpinBox()
        self.year.setRange(1, 9999)
        self.year.setValue(datetime.now().year)
        form.addRow("Год записей", self.year)
        self.cached_account = QLineEdit()
        self.cached_account.setMaxLength(4096)
        self.cached_account.setPlaceholderText("Необязательно; не подтверждает аккаунт")
        form.addRow("Идентификатор из кэша", self.cached_account)
        self.srt = QCheckBox("Анализировать сообщения о потере relay (начальное время: 0)")
        form.addRow(self.srt)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.read_button = QPushButton("Прочитать копию")
        self.cancel_button = QPushButton("Отменить")
        self.read_button.clicked.connect(self._submit)
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.read_button)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.status = QLabel("Результат появится здесь. Он не подтверждает текущее состояние игры.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        layout.addWidget(self.result)
        self.operation_finished.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self.closing_started.connect(self._refresh, Qt.ConnectionType.QueuedConnection)
        self._refresh()

    def _available(self) -> bool:
        return not self._closing and not self.owner._closing and self._job is None

    @Slot()
    def _refresh(self) -> None:
        for control in (
            self.path,
            self.year,
            self.cached_account,
            self.srt,
            self.browse_button,
            self.read_button,
        ):
            control.setEnabled(self._available())
        self.cancel_button.setEnabled(self._job is not None and not self._job.cancelled.is_set())

    def _browse(self) -> None:
        if self._available():
            path, _ = QFileDialog.getOpenFileName(
                self, "Копия консольного лога", "", "Logs (*.log);;All (*)"
            )
            if path:
                self.path.setText(path)

    def _submit(self) -> None:
        if not self._available():
            return
        self.result.clear()
        if not self.path.text():
            self.status.setText("Выберите локальную копию лога.")
            return
        year, srt = self.year.value(), self.srt.isChecked()
        cached = self.cached_account.text() or None
        context = ConsoleParserContext(
            initial_account=cached,
            tail_account=cached,
            fallback_account=1,
            clock=time.time,
            year=lambda: year,
            local_timestamp=datetime.timestamp,
            srt_enabled=lambda: srt,
        )
        job = _ReadJob(self.path.text(), context)
        self._job = job
        self._request += 1
        request = self._request
        job.finished.add_done_callback(lambda done: self.operation_finished.emit(done.result()))
        self.status.setText("Чтение и анализ копии…")
        try:
            self._future = self.owner.bridge.submit(self._perform(job, request))

            def bridge_done(future: concurrent.futures.Future) -> None:
                if future.cancelled():
                    job.cancelled.set()

                    def finish_unstarted() -> None:
                        if job.task is None and not job.finished.done():
                            job.finished.set_result({"request": request, "error": "cancelled"})

                    self.owner.bridge.loop.call_soon_threadsafe(finish_unstarted)

            self._future.add_done_callback(bridge_done)
        except RuntimeError:
            self._closing = True
            self._job = None
            self._future = None
            self.status.setText("Обработчик закрыт.")
        self._refresh()

    async def _perform(self, job: _ReadJob, request: int) -> dict:
        job.task = asyncio.current_task()
        response: dict = {"request": request}
        if job.finished.done():
            return job.finished.result()
        if job.cancelled.is_set():
            response["error"] = "cancelled"
            job.finished.set_result(response)
            return response
        work = asyncio.create_task(
            asyncio.to_thread(self._reader, job.path, job.context, cancelled=job.cancelled)
        )
        try:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    job.cancelled.set()
            result = work.result()
            response["text"] = _summary(result)
            response["issues"] = result.issues
            response["fallback"] = result.legacy_fallback
        except asyncio.CancelledError:
            # An owned reader may itself end cancelled; publish a complete outcome.
            response["error"] = "cancelled"
        except Exception:
            response["error"] = "failed"
        finally:
            if not job.finished.done():
                job.finished.set_result(response)
        return response

    @Slot(object)
    def _completed(self, response: object) -> None:
        if (
            not isinstance(response, dict)
            or self._job is None
            or response.get("request") != self._request
        ):
            return
        self._job = None
        self._future = None
        if response.get("error"):
            self.status.setText(
                "Чтение отменено."
                if response["error"] == "cancelled"
                else "Не удалось прочитать копию. Проверьте путь, доступ и локальное размещение файла."
            )
        else:
            self.result.setPlainText(response["text"])
            if "cancelled" in response["issues"]:
                self.status.setText("Чтение отменено; показан только частичный результат.")
            elif response["fallback"]:
                self.status.setText(
                    "Файл не прочитан надёжно. Показаны резервные значения и диагностика."
                )
            elif response["issues"]:
                self.status.setText("Анализ завершён с замечаниями. Результат может быть неполным.")
            else:
                self.status.setText(
                    "Анализ выбранной копии завершён. Состояние игры не проверялось."
                )
        self._refresh()

    def _cancel(self) -> None:
        if self._job is not None:
            self._job.cancelled.set()
            self.status.setText("Ожидание завершения текущего чтения…")
            self._refresh()

    async def aclose(self) -> None:
        self._closing = True
        self.closing_started.emit()
        job = self._job
        if job is not None:
            job.cancelled.set()
            pending = asyncio.wrap_future(job.finished)
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            pending.result()
            if cancelled:
                raise asyncio.CancelledError()
