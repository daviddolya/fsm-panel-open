"""Explicit preview/apply UI for a copied panel video template."""

import asyncio
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fsm_panel.adapters.video import (
    VideoPreview,
    apply_video_template,
    preview_video_template,
)


class VideoPanel(QWidget):
    def __init__(self, owner) -> None:
        super().__init__()
        self.owner = owner
        self.preview: VideoPreview | None = None
        self._future = None
        self._request = 0
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Выберите копию шаблона панели и существующий cs2_video.txt аккаунта Steam. "
            "Из Steam читаются VendorID и DeviceID. Изменяется только выбранный шаблон; "
            "перед записью создаётся резервная копия. Игра не запускается."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.template_path = QLineEdit()
        self.steam_path = QLineEdit()
        self.browse_buttons = []
        for label, field in (
            ("Копия шаблона панели", self.template_path),
            ("Видео-конфиг Steam (только чтение)", self.steam_path),
        ):
            row = QWidget()
            controls = QHBoxLayout(row)
            controls.setContentsMargins(0, 0, 0, 0)
            controls.addWidget(field)
            browse = QPushButton("Выбрать…")
            browse.clicked.connect(lambda checked=False, target=field: self._browse(target))
            controls.addWidget(browse)
            self.browse_buttons.append(browse)
            form.addRow(label, row)
            field.textChanged.connect(self._invalidate)
        self.policy = QComboBox()
        self.policy.addItem("Только поля VendorID и DeviceID", "fields")
        self.policy.addItem("Совместимость: глобальная замена как в оригинале", "legacy")
        self.policy.currentIndexChanged.connect(self._invalidate)
        form.addRow("Способ замены", self.policy)
        layout.addLayout(form)
        self.policy_note = QLabel()
        self.policy_note.setWordWrap(True)
        layout.addWidget(self.policy_note)
        actions = QHBoxLayout()
        self.preview_button = QPushButton("Предпросмотр")
        self.apply_button = QPushButton("Применить к шаблону")
        self.preview_button.clicked.connect(self._preview)
        self.apply_button.clicked.connect(self._apply)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        layout.addWidget(self.result)
        self.status = QLabel("Выберите файлы для предпросмотра.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        owner.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        owner.bridge.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self._invalidate()
        self._busy(False)

    def _browse(self, field: QLineEdit) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Видео-конфиг", "", "Text (*.txt);;All (*)")
        if filename:
            field.setText(filename)

    def _selection(self) -> tuple[str, str, str]:
        return self.template_path.text(), self.steam_path.text(), self.policy.currentData()

    def _invalidate(self) -> None:
        self.preview = None
        self.apply_button.setEnabled(False)
        self.policy_note.setText(
            "Режим оригинала заменяет цифры во всём шаблоне, включая другие поля. "
            "Проверьте результат перед применением."
            if self.policy.currentData() == "legacy"
            else "Исправление дефекта оригинала: остальные поля и комментарии сохраняются."
        )

    def _busy(self, busy: bool) -> None:
        for control in (
            self.template_path,
            self.steam_path,
            self.policy,
            self.preview_button,
            *self.browse_buttons,
        ):
            control.setEnabled(not busy)
        self.apply_button.setEnabled(not busy and self.preview is not None and self.preview.changed)

    def _preview(self) -> None:
        if self.owner._closing or self._future is not None:
            return
        self._invalidate()
        self.result.clear()
        self.status.setText("Чтение файлов…")
        self._submit("preview", self._selection(), None)

    def _apply(self) -> None:
        if self.owner._closing or self._future is not None or self.preview is None:
            return
        self.status.setText("Проверка файлов и сохранение шаблона…")
        self._submit("apply", self._selection(), self.preview)

    def _submit(self, action: str, selection: tuple[str, str, str], preview: VideoPreview | None):
        self._request += 1
        self._busy(True)
        self._future = self.owner.bridge.submit(
            self._perform(action, selection, preview, self._request)
        )

    async def _perform(self, action, selection, preview, request) -> dict:
        response = {
            "video_panel": id(self),
            "request": request,
            "selection": selection,
            "action": action,
        }
        try:
            if action == "preview":
                if not selection[0] or not selection[1]:
                    response["error"] = "video_paths_required"
                else:
                    response["value"] = await asyncio.to_thread(
                        preview_video_template,
                        Path(selection[0]),
                        Path(selection[1]),
                        policy=selection[2],
                    )
            else:
                response["value"] = await asyncio.to_thread(apply_video_template, preview)
        except Exception as error:
            # Do not copy file contents or arbitrary exception strings into logs.
            response["error"] = getattr(error, "code", "video_operation_failed")
            backup_path = getattr(error, "backup_path", None)
            if backup_path is not None:
                response["backup_path"] = str(backup_path)
        return response

    @Slot(object)
    def _completed(self, response: object) -> None:
        if not isinstance(response, dict) or response.get("video_panel") != id(self):
            return
        if response["request"] != self._request:
            return
        self._future = None
        if "error" in response:
            self.preview = None
            self.status.setText(
                f"Операция не выполнена: {response['error']}. Повторите предпросмотр."
                + (
                    f" Резервная копия: {response['backup_path']}"
                    if "backup_path" in response
                    else ""
                )
            )
        elif response["action"] == "preview":
            if response["selection"] != self._selection():
                self.preview = None
                self.status.setText("Выбор файлов изменился. Повторите предпросмотр.")
            else:
                self.preview = response["value"]
                value = self.preview
                self.result.setPlainText(value.rendered_content)
                self.status.setText(
                    f"VendorID: {value.before_vendor_id} → {value.after_vendor_id}; "
                    f"DeviceID: {value.before_device_id} → {value.after_device_id}. "
                    + (
                        "Проверьте новый текст шаблона перед применением."
                        if value.changed
                        else "Изменений нет; запись не требуется."
                    )
                )
        else:
            applied = response["value"]
            self.preview = None
            self.status.setText(
                f"Шаблон сохранён. Резервная копия: {applied.backup_path}"
                if applied.changed
                else "Изменений нет; запись не выполнялась."
            )
        self._busy(False)

    @Slot(str)
    def _failed(self, code: str) -> None:
        if (
            self._future is not None
            and self._future.done()
            and (self._future.cancelled() or self._future.exception() is not None)
        ):
            self._future = None
            self.preview = None
            self.status.setText("Операция прервана. Проверьте файлы и повторите предпросмотр.")
            self._busy(False)

    async def aclose(self) -> None:
        pending = self._future
        if pending is not None:
            # Atomic file work must finish before the worker thread shuts down.
            await asyncio.wrap_future(pending)
