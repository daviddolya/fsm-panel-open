"""Explicit offline composition, owned until its file worker has completed."""

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fsm_panel.notifications.images import ItemCompositeError, compose_item_image_files


@dataclass
class _ImageJob:
    first: str = field(repr=False)
    second: str = field(repr=False)
    output: str = field(repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    finished: concurrent.futures.Future = field(
        default_factory=concurrent.futures.Future, repr=False
    )


class ItemCompositePanel(QWidget):
    closing_started = Signal()
    operation_finished = Signal(object)

    def __init__(self, owner, *, composer=compose_item_image_files) -> None:
        super().__init__()
        self.owner = owner
        self._composer = composer
        self._future: concurrent.futures.Future | None = None
        self._job: _ImageJob | None = None
        self._request = 0
        self._closing = False
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Выберите два локальных изображения PNG или JPEG. Второе будет растянуто до "
            "размера первого и помещено справа. Сохранение создаёт новый PNG; "
            "существующий файл не заменяется. До 8 МиБ и 4 млн пикселей на изображение, "
            "без анимации и 16-битного цвета. Допуск не нужен."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.first_path = QLineEdit()
        self.second_path = QLineEdit()
        self.output_path = QLineEdit()
        self.browse_buttons = []
        for label, control, output in (
            ("Первое изображение", self.first_path, False),
            ("Второе изображение", self.second_path, False),
            ("Новый PNG", self.output_path, True),
        ):
            row = QHBoxLayout()
            row.addWidget(control)
            button = QPushButton("Выбрать…")
            button.clicked.connect(
                lambda checked=False, field=control, save=output: self._browse(field, save)
            )
            self.browse_buttons.append(button)
            row.addWidget(button)
            form.addRow(label, row)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.compose_button = QPushButton("Создать PNG")
        self.cancel_button = QPushButton("Отменить")
        self.compose_button.clicked.connect(self._submit)
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.compose_button)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.status = QLabel("Файл можно затем выбрать вручную для отправки во вкладке Telegram.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        owner.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        owner.bridge.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.closing_started.connect(self._refresh, Qt.ConnectionType.QueuedConnection)
        self.operation_finished.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._refresh()

    def _available(self) -> bool:
        return not self._closing and not self.owner._closing and self._future is None

    @Slot()
    def _refresh(self) -> None:
        for control in (
            self.first_path,
            self.second_path,
            self.output_path,
            self.compose_button,
            *self.browse_buttons,
        ):
            control.setEnabled(self._available())
        self.cancel_button.setEnabled(
            self._future is not None and self._job is not None and not self._job.cancelled.is_set()
        )

    def _browse(self, field: QLineEdit, save: bool) -> None:
        if not self._available():
            return
        if save:
            path, _ = QFileDialog.getSaveFileName(self, "Новый файл", "", "PNG (*.png)")
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Изображение", "", "PNG/JPEG (*.png *.jpg *.jpeg)"
            )
        if path:
            field.setText(path)

    def _submit(self) -> None:
        if not self._available():
            return
        if not all((self.first_path.text(), self.second_path.text(), self.output_path.text())):
            self.status.setText("Выберите два изображения и путь нового PNG.")
            return
        self._request += 1
        self._job = _ImageJob(
            self.first_path.text(), self.second_path.text(), self.output_path.text()
        )
        self.status.setText("Чтение изображений и создание PNG…")
        try:
            job, request = self._job, self._request
            job.finished.add_done_callback(lambda done: self.operation_finished.emit(done.result()))
            self._future = self.owner.bridge.submit(self._perform(job, request))

            def bridge_done(future: concurrent.futures.Future) -> None:
                if future.cancelled():
                    job.cancelled.set()

                    def finish_unstarted() -> None:
                        # run_coroutine_threadsafe also cancels a not-yet-started task.
                        # Its body/finally cannot publish completion in that case.
                        if job.task is None and not job.finished.done():
                            job.finished.set_result(
                                {
                                    "item_composite_panel": id(self),
                                    "request": request,
                                    "error": "cancelled",
                                }
                            )

                    self.owner.bridge.loop.call_soon_threadsafe(finish_unstarted)

            self._future.add_done_callback(bridge_done)
        except RuntimeError:
            self._closing = True
            self._job = None
            self.status.setText("Обработчик закрыт.")
        self._refresh()

    async def _perform(self, job: _ImageJob, request: int) -> dict:
        job.task = asyncio.current_task()
        response: dict = {"item_composite_panel": id(self), "request": request}
        if job.finished.done():
            return job.finished.result()
        if job.cancelled.is_set():
            response["error"] = "cancelled"
            job.finished.set_result(response)
            return response
        work = asyncio.create_task(
            asyncio.to_thread(
                self._composer, job.first, job.second, job.output, cancelled=job.cancelled
            )
        )
        # Cancelling asyncio.to_thread alone cannot stop a writer. Retain ownership
        # and report its actual outcome, including a successful committed write.
        try:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    job.cancelled.set()
            result = work.result()
            response["size"] = (result.width, result.height)
        except ItemCompositeError as error:
            response["error"] = {
                "image_cancelled": "cancelled",
                "image_output_exists": "exists",
                "image_output_cleanup_failed": "cleanup_failed",
            }.get(error.code, "failed")
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
            or response.get("item_composite_panel") != id(self)
            or response.get("request") != self._request
        ):
            return
        self._future = None
        self._job = None
        error = response.get("error")
        if error:
            self.status.setText(
                {
                    "cancelled": "Создание отменено. Новый файл не создан.",
                    "exists": "Файл уже существует. Выберите новое имя PNG.",
                    "cleanup_failed": "Ошибка записи и очистки. Проверьте выбранный выходной файл.",
                    "failed": "Не удалось создать PNG. Проверьте формат, размеры и доступ к локальным файлам.",
                }[error]
            )
        else:
            width, height = response["size"]
            self.status.setText(f"PNG сохранён: {width} × {height}. Отправка выполняется вручную.")
        self._refresh()

    @Slot(str)
    def _failed(self, code: str) -> None:
        # The bridge caller Future can be cancelled before its owned thread ends.
        # Only the independent job.finished signal can release these controls.
        if self._job is not None:
            return
        pending = self._future
        if (
            pending is not None
            and pending.done()
            and (pending.cancelled() or pending.exception() is not None)
        ):
            self._future = None
            self._job = None
            self._closing = True
            self.status.setText("Обработчик прерван. Проверьте выбранный выходной файл.")
            self._refresh()

    def _cancel(self) -> None:
        if self._job is not None:
            self._job.cancelled.set()
            self.status.setText(
                "Ожидание завершения текущего шага; начатая запись будет завершена…"
            )
            self._refresh()

    async def aclose(self) -> None:
        self._closing = True
        self.closing_started.emit()
        if self._job is not None:
            self._job.cancelled.set()
        if self._job is not None:
            pending = asyncio.wrap_future(self._job.finished)
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            pending.result()
            if cancelled:
                raise asyncio.CancelledError()
