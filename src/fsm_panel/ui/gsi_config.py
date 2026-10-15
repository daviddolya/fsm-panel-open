"""Reviewed GSI writes to an existing offline copy, using the owner's bridge."""

import asyncio
import concurrent.futures
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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fsm_panel.adapters.account_config import (
    ConfigApplyError,
    ConfigApplyResult,
    ConfigError,
    ConfigStalePreviewError,
    open_config_workspace,
)
from fsm_panel.adapters.gsi_config import (
    GsiConfigPreview,
    apply_gsi_config,
    preview_gsi_config,
)
from fsm_panel.config_cli import preview_fingerprint

type _Selection = tuple[str, str, int, int, str]

# Only known adapter codes are displayed; never echo arbitrary exception fields.
_ERROR_CODES = frozenset(
    {
        "gsi_inputs_required",
        "gsi_port_invalid",
        "gsi_index_invalid",
        "gsi_token_invalid",
        "gsi_config_directory_missing",
        "gsi_template_unavailable",
        "gsi_template_hash_mismatch",
        "config_preview_stale",
        "config_link_rejected",
        "config_reparse_point_rejected",
        "config_hardlink_rejected",
        "config_special_file_rejected",
        "config_original_distribution_rejected",
        "config_live_steam_ancestor_rejected",
        "config_workspace_marker_required",
        "config_workspace_marker_invalid",
        "config_invalid_relative_path",
        "config_path_escape",
        "config_invalid_filename",
        "config_regular_file_required",
        "config_file_too_large",
        "config_tree_too_large",
        "config_decode_failed",
        "config_encode_failed",
        "config_directory_required",
        "config_source_destination_overlap",
    }
)


@dataclass(frozen=True)
class _Reviewed:
    preview: GsiConfigPreview = field(repr=False)
    fingerprint: str


def _preview_selection(selection: _Selection) -> _Reviewed:
    workspace_path, source, port, index, encoding = selection
    if not workspace_path or not source or not encoding:
        raise ConfigError("gsi_inputs_required")
    workspace = open_config_workspace(workspace_path)
    proposal = preview_gsi_config(
        workspace,
        running_on_port=port,
        gsi_file_index=index,
        token_source=source,
        encoding=encoding,
    )
    return _Reviewed(proposal, preview_fingerprint(proposal))


def _apply_reviewed(reviewed: _Reviewed) -> ConfigApplyResult:
    if preview_fingerprint(reviewed.preview) != reviewed.fingerprint:
        raise ConfigStalePreviewError()
    return apply_gsi_config(reviewed.preview)


class GsiConfigPanel(QWidget):
    """The owner must await aclose before shutting down its shared AsyncBridge."""

    closing_started = Signal()

    def __init__(self, owner) -> None:
        super().__init__()
        self.owner = owner
        self.preview: GsiConfigPreview | None = None
        self.fingerprint: str | None = None
        self._review_selection: _Selection | None = None
        self._future: concurrent.futures.Future | None = None
        self._request = 0
        self._generation = 0
        self._closing = False
        layout = QVBoxLayout(self)
        self.notice = QLabel(
            "Запись GSI только в отдельную рабочую копию. Создайте её на вкладке "
            "«Конфиги игры», затем выберите здесь. Папка game/game/csgo/cfg должна существовать. "
            "Порт не проверяется, сервер не запускается. Токен читается из отдельного файла "
            "вне рабочей копии; содержимое токена не показывается."
        )
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        form = QFormLayout()
        self.workspace_path = QLineEdit()
        self.token_source = QLineEdit()
        self.browse_buttons: list[QPushButton] = []
        for label, target, directory in (
            ("Рабочая копия", self.workspace_path, True),
            ("Файл токена (только чтение)", self.token_source, False),
        ):
            row = QWidget()
            controls = QHBoxLayout(row)
            controls.setContentsMargins(0, 0, 0, 0)
            controls.addWidget(target)
            browse = QPushButton("Выбрать…")
            browse.clicked.connect(
                lambda checked=False, field=target, folder=directory: self._browse(field, folder)
            )
            controls.addWidget(browse)
            self.browse_buttons.append(browse)
            form.addRow(label, row)
            target.textChanged.connect(self._invalidate)
        self.running_on_port = QSpinBox()
        self.running_on_port.setRange(1, 65535)
        self.running_on_port.setValue(6969)
        self.gsi_file_index = QSpinBox()
        self.gsi_file_index.setRange(1, 10)
        self.gsi_file_index.setValue(1)
        self.encoding = QComboBox()
        self.encoding.setEditable(True)
        self.encoding.addItems(["utf-8", "utf-8-sig", "cp1251", "utf-16"])
        form.addRow("Порт назначения", self.running_on_port)
        form.addRow("Индекс файла", self.gsi_file_index)
        form.addRow("Кодировка", self.encoding)
        self.running_on_port.valueChanged.connect(self._invalidate)
        self.gsi_file_index.valueChanged.connect(self._invalidate)
        self.encoding.currentTextChanged.connect(self._invalidate)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.preview_button = QPushButton("Предпросмотр")
        self.apply_button = QPushButton("Применить к копии")
        self.preview_button.clicked.connect(self._preview)
        self.apply_button.clicked.connect(self._apply)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(150)
        layout.addWidget(self.summary)
        self.status = QLabel("Выберите рабочую копию и файл токена, затем выполните предпросмотр.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        owner.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        owner.bridge.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.closing_started.connect(self._refresh_controls, Qt.ConnectionType.QueuedConnection)
        self._refresh_controls()

    def _browse(self, target: QLineEdit, directory: bool) -> None:
        if directory:
            path = QFileDialog.getExistingDirectory(self, "Рабочая копия")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Файл токена", "", "All (*)")
        if path:
            target.setText(path)

    def _selection(self) -> _Selection:
        return (
            self.workspace_path.text(),
            self.token_source.text(),
            self.running_on_port.value(),
            self.gsi_file_index.value(),
            self.encoding.currentText(),
        )

    def _invalidate(self) -> None:
        self._generation += 1
        self.preview = None
        self.fingerprint = None
        self._review_selection = None
        self.summary.clear()
        self.status.setText("Параметры изменились. Выполните новый предпросмотр.")
        self._refresh_controls()

    def _available(self) -> bool:
        return not self._closing and not self.owner._closing and self._future is None

    @Slot()
    def _refresh_controls(self) -> None:
        ready = self._available()
        for control in (
            self.workspace_path,
            self.token_source,
            self.running_on_port,
            self.gsi_file_index,
            self.encoding,
            *self.browse_buttons,
        ):
            control.setEnabled(ready)
        self.preview_button.setEnabled(
            ready and bool(self.workspace_path.text() and self.token_source.text())
        )
        self.apply_button.setEnabled(ready and self.preview is not None)

    def _preview(self) -> None:
        if not self._available():
            return
        self._invalidate()
        self.status.setText("Чтение токена и проверка рабочей копии…")
        self._submit("preview", self._selection())

    def _apply(self) -> None:
        if not self._available() or self.preview is None or self.fingerprint is None:
            return
        if self._review_selection != self._selection():
            self._invalidate()
            return
        self.status.setText("Повторная проверка и сохранение GSI…")
        self._submit("apply", _Reviewed(self.preview, self.fingerprint))

    def _submit(self, action: str, payload: object) -> None:
        self._request += 1
        try:
            self._future = self.owner.bridge.submit(
                self._perform(action, payload, self._request, self._generation)
            )
        except RuntimeError:
            self._closing = True
            self.status.setText("Обработчик закрыт. Новые операции недоступны.")
        self._refresh_controls()

    async def _perform(self, action, payload, request, generation) -> dict:
        response = {
            "gsi_config_panel": id(self),
            "request": request,
            "generation": generation,
            "action": action,
        }
        try:
            response["value"] = await asyncio.to_thread(
                _preview_selection if action == "preview" else _apply_reviewed, payload
            )
            if action == "preview":
                response["selection"] = payload
        except ConfigApplyError as error:
            response.update(
                error="config_apply_failed"
                if error.rollback_succeeded
                else "config_rollback_failed",
                backup=error.backup_path,
                staging=error.staging_path,
                rollback=error.rollback_succeeded,
            )
        except ConfigError as error:
            response["error"] = (
                error.code if error.code in _ERROR_CODES else "config_operation_failed"
            )
        except OSError:
            response["error"] = "config_file_unavailable"
        except LookupError, ValueError, UnicodeError:
            response["error"] = "config_invalid_input"
        except Exception:
            response["error"] = "config_operation_failed"
        return response

    @Slot(object)
    def _completed(self, response: object) -> None:
        if (
            not isinstance(response, dict)
            or response.get("gsi_config_panel") != id(self)
            or response.get("request") != self._request
        ):
            return
        self._future = None
        if "error" in response:
            self._invalidate()
            message = f"Операция не выполнена: {response['error']}. Повторите предпросмотр."
            if response.get("backup"):
                message += f" Резервная копия: {response['backup']}."
            if response.get("staging"):
                message += f" Оставшаяся рабочая папка: {response['staging']}."
            if "rollback" in response:
                message += (
                    " Исходная копия восстановлена."
                    if response["rollback"]
                    else " Откат не завершён; сохраните папки для восстановления."
                )
            self.status.setText(message)
        elif response["action"] == "preview":
            if (
                self._closing
                or self.owner._closing
                or response["generation"] != self._generation
                or response["selection"] != self._selection()
            ):
                self._invalidate()
                self.status.setText(
                    "Параметры изменились или панель закрывается. Повторите предпросмотр."
                )
            else:
                reviewed = response["value"]
                self.preview, self.fingerprint = reviewed.preview, reviewed.fingerprint
                self._review_selection = response["selection"]
                self.summary.setPlainText(
                    f"Файл: {self.preview.target_path.name}\n"
                    f"Записей: {len(self.preview.transaction.writes)}; "
                    f"удалений: {len(self.preview.transaction.removals)}.\n"
                    f"Отпечаток плана: {self.fingerprint}\n"
                    "Файл перезаписывается и при совпадении содержимого. Другие индексы сохраняются."
                )
                self.status.setText("Предпросмотр готов. Применение изменит только рабочую копию.")
        else:
            applied = response["value"]
            self._invalidate()
            self.status.setText(f"GSI сохранён в копии. Резервная копия: {applied.backup_path}")
        self._refresh_controls()

    @Slot(str)
    def _failed(self, code: str) -> None:
        pending = self._future
        if (
            pending is not None
            and pending.done()
            and (pending.cancelled() or pending.exception() is not None)
        ):
            self._future = None
            self._invalidate()
            self.status.setText("Операция прервана. Проверьте файлы и повторите предпросмотр.")

    async def aclose(self) -> None:
        """Join file work without cancelling a thread during tree replacement."""
        self._closing = True
        self.closing_started.emit()
        pending = self._future
        if pending is not None:
            await asyncio.shield(asyncio.wrap_future(pending))
