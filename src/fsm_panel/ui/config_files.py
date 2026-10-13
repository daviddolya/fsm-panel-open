"""Standalone Qt controls for explicit, isolated configuration copies.

All filesystem and adapter calls run in the bridge's executor. Only queued Qt
slots update controls; the widget neither reads MainWindow settings nor launches
external applications.
"""

import asyncio
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
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

from fsm_panel.adapters.account_config import (
    AccountConfigPreview,
    AccountConfigSettings,
    ConfigApplyError,
    ConfigApplyResult,
    ConfigError,
    ConfigStalePreviewError,
    ConfigWorkspace,
    SourceSnapshot,
    apply_account_config,
    create_config_workspace,
    open_config_workspace,
    preview_account_config,
    read_config_input,
)
from fsm_panel.adapters.autoexec import AutoexecPreview, apply_autoexec, preview_autoexec
from fsm_panel.config_cli import preview_fingerprint
from fsm_panel.core import SettingsStore
from fsm_panel.core.models import ValidationError
from fsm_panel.ui.bridge import AsyncBridge

type Workflow = Literal["account", "autoexec"]
type Preview = AccountConfigPreview | AutoexecPreview


@dataclass(frozen=True)
class _ImportedSettings:
    path: Path = field(repr=False)
    snapshot: SourceSnapshot = field(repr=False)
    steam_path: str = field(repr=False)
    disable_overlay: bool
    no_avast: bool


@dataclass(frozen=True)
class _Reviewed:
    preview: Preview = field(repr=False)
    fingerprint: str
    auxiliary_sources: tuple[tuple[Path, str, SourceSnapshot], ...] = field(repr=False)


def _load_settings(path: str, workspace: ConfigWorkspace) -> _ImportedSettings:
    source_path = Path(path).absolute()
    text, snapshot = read_config_input(source_path, workspace, "utf-8-sig")
    document = SettingsStore.loads(text)
    steam = document.get("STEAM_PATH")
    overlay = document.get("DISABLE_STEAM_OVERLAY")
    no_avast = document.get("NO_AVAST_MODE")
    if type(steam) is not str or type(overlay) is not bool or type(no_avast) is not bool:
        raise ConfigError("config_required_settings_missing")
    return _ImportedSettings(source_path, snapshot, steam, overlay, no_avast)


def _account_preview(
    workspace: ConfigWorkspace,
    imported: _ImportedSettings,
    values: tuple[str, str, str, str, str, str, str, bool],
) -> _Reviewed:
    steam_id, login, video, machine, timestamp, localconfig, encoding, force = values
    if not all((steam_id, login, video, machine, timestamp, encoding)):
        raise ConfigError("config_account_inputs_required")
    current = _load_settings(str(imported.path), workspace)
    if current != imported:
        raise ConfigStalePreviewError()
    mapped = None
    auxiliary = [(imported.path, "utf-8-sig", imported.snapshot)]
    if localconfig:
        path = Path(localconfig).absolute()
        mapped, snapshot = read_config_input(path, workspace, encoding)
        auxiliary.append((path, encoding, snapshot))
    proposal = preview_account_config(
        workspace,
        AccountConfigSettings(
            steam_path=imported.steam_path,
            login=login,
            force=force,
            disable_steam_overlay=imported.disable_overlay,
            no_avast_mode=imported.no_avast,
            mapped_localconfig=mapped,
        ),
        steam_id=int(steam_id),
        video_source=video,
        machine_source=machine,
        now=float(timestamp),
        encoding=encoding,
        auxiliary_sources=tuple(snapshot for _, _, snapshot in auxiliary),
    )
    return _Reviewed(proposal, preview_fingerprint((proposal, auxiliary)), tuple(auxiliary))


def _autoexec_preview(workspace: ConfigWorkspace, values: tuple[str, str, str]) -> _Reviewed:
    source, name, encoding = values
    if not all(values):
        raise ConfigError("config_autoexec_inputs_required")
    proposal = preview_autoexec(
        workspace, source_path=source, config_file_name=name, encoding=encoding
    )
    return _Reviewed(proposal, preview_fingerprint((proposal, ())), ())


def _apply_reviewed(reviewed: _Reviewed) -> ConfigApplyResult:
    if preview_fingerprint((reviewed.preview, reviewed.auxiliary_sources)) != reviewed.fingerprint:
        raise ConfigStalePreviewError()
    for path, encoding, snapshot in reviewed.auxiliary_sources:
        try:
            _, current = read_config_input(path, reviewed.preview.workspace, encoding)
        except OSError, ConfigError, ValueError:
            raise ConfigStalePreviewError() from None
        if current != snapshot:
            raise ConfigStalePreviewError()
    if isinstance(reviewed.preview, AccountConfigPreview):
        return apply_account_config(reviewed.preview)
    return apply_autoexec(reviewed.preview)


class _WorkflowControls(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.layout_box = QVBoxLayout(self)
        self.form = QFormLayout()
        self.layout_box.addLayout(self.form)
        actions = QHBoxLayout()
        self.preview_button = QPushButton("Предпросмотр")
        self.apply_button = QPushButton("Применить к копии")
        self.apply_button.setEnabled(False)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.apply_button)
        self.layout_box.addLayout(actions)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(150)
        self.layout_box.addWidget(self.summary)
        self.status = QLabel("Откройте рабочую копию и задайте параметры.")
        self.status.setWordWrap(True)
        self.layout_box.addWidget(self.status)
        self.layout_box.addStretch()


class ConfigFilesWidget(QWidget):
    """An optional shared bridge is the only required integration contract.

    The owner must await ``aclose`` before shutting down a supplied bridge. With no
    bridge argument, closing this standalone widget shuts its own bridge down after
    all pending filesystem work has completed.
    """

    closing_started = Signal()

    def __init__(self, bridge: AsyncBridge | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge if bridge is not None else AsyncBridge()
        self._owns_bridge = bridge is None
        self._future: concurrent.futures.Future | None = None
        self._closing = False
        self._closed = False
        self._request = 0
        self._generation = 0
        self.workspace: ConfigWorkspace | None = None
        self.imported_settings: _ImportedSettings | None = None
        self.account_review: _Reviewed | None = None
        self.autoexec_review: _Reviewed | None = None
        self._controls: list[QWidget] = []
        self.browse_buttons: list[QPushButton] = []
        layout = QVBoxLayout(self)
        notice = QLabel(
            "Подготовка конфигов в отдельной рабочей копии. Создайте новую папку или "
            "откройте ранее созданную здесь копию. Предпросмотр показывает объём изменений; "
            "применение сохраняет предыдущие файлы в резервной копии. Steam и игра не запускаются."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        workspace_row = QHBoxLayout()
        self.workspace_path = QLineEdit()
        self.workspace_path.setPlaceholderText("Путь к отдельной рабочей папке")
        self.workspace_path.textChanged.connect(self._workspace_edited)
        self.create_workspace_button = QPushButton("Создать новую")
        self.create_workspace_button.setToolTip(
            "Укажите новую папку внутри существующей папки. Готовая папка не принимается."
        )
        self.open_workspace_button = QPushButton("Открыть копию")
        self.workspace_browse_button = QPushButton("Выбрать…")
        self.workspace_browse_button.clicked.connect(self._browse_workspace)
        self.create_workspace_button.clicked.connect(lambda: self._workspace_action("create"))
        self.open_workspace_button.clicked.connect(lambda: self._workspace_action("open"))
        for control in (
            self.workspace_path,
            self.workspace_browse_button,
            self.create_workspace_button,
            self.open_workspace_button,
        ):
            workspace_row.addWidget(control)
            self._controls.append(control)
        layout.addLayout(workspace_row)
        self.workspace_status = QLabel("Рабочая копия не открыта.")
        self.workspace_status.setWordWrap(True)
        layout.addWidget(self.workspace_status)
        self.tabs = QTabWidget()
        self.account = _WorkflowControls()
        self.autoexec = _WorkflowControls()
        self.tabs.addTab(self.account, "Конфиги аккаунта")
        self.tabs.addTab(self.autoexec, "Autoexec")
        layout.addWidget(self.tabs)
        self.settings_path = self._file_field(
            self.account.form, "Файл настроек JSON", changed=self._settings_edited
        )
        self.import_settings_button = QPushButton("Импортировать настройки")
        self.import_settings_button.clicked.connect(self._import_settings)
        self.account.form.addRow(self.import_settings_button)
        self._controls.append(self.import_settings_button)
        self.settings_status = QLabel(
            "Нужны STEAM_PATH, DISABLE_STEAM_OVERLAY и NO_AVAST_MODE из явного импорта."
        )
        self.settings_status.setWordWrap(True)
        self.account.form.addRow(self.settings_status)
        self.steam_id = self._text_field(self.account.form, "Steam ID")
        self.login = self._text_field(self.account.form, "Логин")
        self.video_source = self._file_field(self.account.form, "Шаблон видео")
        self.machine_source = self._file_field(self.account.form, "Шаблон machine-convars")
        self.timestamp = self._text_field(self.account.form, "Время Unix для плана")
        self.localconfig_copy = self._file_field(
            self.account.form, "Копия localconfig (необязательно)"
        )
        self.account_encoding = self._encoding_field(self.account.form)
        self.force = QCheckBox("Force: скрыть сообщения оригинала (файлы изменяются так же)")
        self.force.toggled.connect(self._inputs_edited)
        self.account.form.addRow(self.force)
        self._controls.append(self.force)
        self.autoexec_source = self._file_field(self.autoexec.form, "Шаблон autoexec")
        self.config_file_name = self._text_field(
            self.autoexec.form, "CONFIG_FILE_NAME (указать явно)"
        )
        self.autoexec_encoding = self._encoding_field(self.autoexec.form)
        autoexec_note = QLabel(
            "В копии должна существовать папка game/game/csgo/cfg. При полном совпадении "
            "текста запись не нужна; файл с указанным CONFIG_FILE_NAME тоже сохраняется."
        )
        autoexec_note.setWordWrap(True)
        self.autoexec.form.addRow(autoexec_note)
        for kind, controls in (("account", self.account), ("autoexec", self.autoexec)):
            controls.preview_button.clicked.connect(
                lambda checked=False, selected=kind: self._preview(selected)
            )
            controls.apply_button.clicked.connect(
                lambda checked=False, selected=kind: self._apply(selected)
            )
            self._controls.append(controls.preview_button)
        self.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self.bridge.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.closing_started.connect(self._closing_ui, Qt.ConnectionType.QueuedConnection)
        if self._owns_bridge:
            self.bridge.shutdown_finished.connect(self._finish_close)
        self._refresh_controls()

    def _text_field(self, form: QFormLayout, label: str) -> QLineEdit:
        field = QLineEdit()
        field.textChanged.connect(self._inputs_edited)
        self._controls.append(field)
        form.addRow(label, field)
        return field

    def _file_field(self, form: QFormLayout, label: str, *, changed=None) -> QLineEdit:
        field = QLineEdit()
        field.textChanged.connect(changed or self._inputs_edited)
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(lambda: self._browse_file(field))
        self._controls.extend((field, browse))
        self.browse_buttons.append(browse)
        layout.addWidget(field)
        layout.addWidget(browse)
        form.addRow(label, row)
        return field

    def _encoding_field(self, form: QFormLayout) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(["utf-8", "cp1251", "cp1252"])
        combo.currentTextChanged.connect(self._inputs_edited)
        self._controls.append(combo)
        form.addRow("Кодировка источников", combo)
        return combo

    def _browse_file(self, field: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Источник (только чтение)", "", "All (*)")
        if path:
            field.setText(path)

    def _browse_workspace(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Ранее созданная рабочая копия")
        if path:
            self.workspace_path.setText(path)

    def _workspace_edited(self) -> None:
        self.workspace = None
        self.imported_settings = None
        self._inputs_edited()
        self.workspace_status.setText("Откройте или создайте выбранную рабочую копию.")
        if hasattr(self, "settings_status"):
            self.settings_status.setText("Повторно импортируйте настройки для выбранной копии.")

    def _settings_edited(self) -> None:
        self.imported_settings = None
        self._inputs_edited()
        self.settings_status.setText("Импортируйте выбранный файл настроек.")

    def _inputs_edited(self) -> None:
        self._generation += 1
        self.account_review = None
        self.autoexec_review = None
        for controls in (self.account, self.autoexec):
            controls.summary.clear()
            controls.status.setText("Параметры изменены. Выполните новый предпросмотр.")
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        idle = self._future is None and not self._closing
        for control in self._controls:
            control.setEnabled(idle)
        if hasattr(self, "import_settings_button"):
            self.import_settings_button.setEnabled(idle and self.workspace is not None)
        if hasattr(self, "account"):
            self.account.preview_button.setEnabled(
                idle and self.workspace is not None and self.imported_settings is not None
            )
            self.account.apply_button.setEnabled(idle and self.account_review is not None)
            self.autoexec.preview_button.setEnabled(idle and self.workspace is not None)
            self.autoexec.apply_button.setEnabled(
                idle
                and self.autoexec_review is not None
                and isinstance(self.autoexec_review.preview, AutoexecPreview)
                and self.autoexec_review.preview.plan.changed
            )

    def _available(self) -> bool:
        return not self._closing and self._future is None

    def _workspace_action(self, action: str) -> None:
        if not self._available():
            return
        path = self.workspace_path.text().strip()
        if not path:
            self.workspace_status.setText("Укажите путь к рабочей копии.")
            return
        self.workspace = None
        self.imported_settings = None
        self._inputs_edited()
        self.workspace_status.setText("Подготовка рабочей копии…")
        self._submit("workspace_" + action, None, path)

    def _import_settings(self) -> None:
        if not self._available() or self.workspace is None:
            return
        if not self.settings_path.text().strip():
            self.settings_status.setText("Укажите файл настроек.")
            return
        self.imported_settings = None
        self._inputs_edited()
        self.settings_status.setText("Чтение выбранных настроек…")
        self._submit("settings", None, (self.settings_path.text(), self.workspace))

    def _preview(self, kind: Workflow) -> None:
        if not self._available() or self.workspace is None:
            return
        controls = self.account if kind == "account" else self.autoexec
        payload: object
        if kind == "account":
            if self.imported_settings is None:
                return
            self.account_review = None
            values = (
                self.steam_id.text(),
                self.login.text(),
                self.video_source.text(),
                self.machine_source.text(),
                self.timestamp.text(),
                self.localconfig_copy.text(),
                self.account_encoding.currentText(),
                self.force.isChecked(),
            )
            payload = (self.workspace, self.imported_settings, values)
        else:
            self.autoexec_review = None
            payload = (
                self.workspace,
                (
                    self.autoexec_source.text(),
                    self.config_file_name.text(),
                    self.autoexec_encoding.currentText(),
                ),
            )
        controls.summary.clear()
        controls.status.setText("Чтение источников и проверка рабочей копии…")
        self._submit("preview", kind, payload)

    def _apply(self, kind: Workflow) -> None:
        if not self._available():
            return
        reviewed = self.account_review if kind == "account" else self.autoexec_review
        if reviewed is None:
            return
        controls = self.account if kind == "account" else self.autoexec
        controls.status.setText("Повторная проверка и сохранение копии…")
        self._submit("apply", kind, reviewed)

    def _submit(self, action: str, kind: Workflow | None, payload: object) -> None:
        self._request += 1
        try:
            self._future = self.bridge.submit(
                self._perform(action, kind, payload, self._request, self._generation)
            )
        except RuntimeError:
            self._closing = True
            self.workspace_status.setText("Обработчик закрыт. Новые операции недоступны.")
        self._refresh_controls()

    async def _perform(self, action, kind, payload, request, generation) -> dict:
        response: dict[str, object] = {
            "config_files_widget": id(self),
            "request": request,
            "generation": generation,
            "action": action,
            "kind": kind,
        }
        try:
            value: object
            if action == "workspace_create":
                value = await asyncio.to_thread(create_config_workspace, payload)
            elif action == "workspace_open":
                value = await asyncio.to_thread(open_config_workspace, payload)
            elif action == "settings":
                value = await asyncio.to_thread(_load_settings, *payload)
            elif action == "preview":
                value = await asyncio.to_thread(
                    _account_preview if kind == "account" else _autoexec_preview, *payload
                )
            else:
                value = await asyncio.to_thread(_apply_reviewed, payload)
            response["value"] = value
        except ConfigApplyError as error:
            response.update(
                error=error.code,
                backup=error.backup_path,
                staging=error.staging_path,
                rollback=error.rollback_succeeded,
            )
        except ConfigError as error:
            response["error"] = error.code
        except ValidationError:
            response["error"] = "config_invalid_settings"
        except LookupError, ValueError, UnicodeError:
            response["error"] = "config_invalid_input"
        except OSError:
            response["error"] = "config_file_unavailable"
        except Exception:
            response["error"] = "config_operation_failed"
        return response

    @Slot(object)
    def _completed(self, response: object) -> None:
        if not isinstance(response, dict):
            return
        if response.get("config_files_closed") == id(self):
            self._finish_close()
            return
        if (
            response.get("config_files_widget") != id(self)
            or response.get("request") != self._request
        ):
            return
        self._future = None
        action = response["action"]
        kind = response["kind"]
        controls = self.account if kind == "account" else self.autoexec
        if action.startswith("workspace_"):
            status = self.workspace_status
        elif action == "settings":
            status = self.settings_status
        else:
            status = controls.status
        if "error" in response:
            self.account_review = None
            self.autoexec_review = None
            self.account.summary.clear()
            self.autoexec.summary.clear()
            message = f"Операция не выполнена: {response['error']}. Повторите предпросмотр."
            if response["error"] == "config_preview_stale" and kind == "account":
                message += " Если изменился файл настроек, импортируйте его повторно."
            if response.get("backup"):
                message += f" Резервная копия: {response['backup']}."
            if response.get("staging"):
                message += f" Оставшаяся рабочая папка: {response['staging']}."
            if "rollback" in response:
                message += (
                    " Исходная копия восстановлена."
                    if response["rollback"]
                    else " Откат не завершён; сохранена резервная копия."
                )
            status.setText(message)
        elif action != "apply" and response["generation"] != self._generation:
            status.setText(
                "Параметры изменились во время операции. Повторите выбор и предпросмотр."
            )
        elif action.startswith("workspace_"):
            self.workspace = response["value"]
            self.workspace_status.setText(
                "Изолированная рабочая копия открыта. Источники выбираются отдельно."
            )
        elif action == "settings":
            imported = response["value"]
            self.imported_settings = imported
            self.settings_status.setText(
                "Настройки импортированы. DISABLE_STEAM_OVERLAY: "
                + ("да" if imported.disable_overlay else "нет")
                + "; NO_AVAST_MODE: "
                + ("да" if imported.no_avast else "нет")
                + "."
            )
        elif action == "preview":
            reviewed = response["value"]
            if kind == "account":
                self.account_review = reviewed
            else:
                self.autoexec_review = reviewed
            proposal = reviewed.preview
            lines = [
                f"Отпечаток плана: {reviewed.fingerprint}",
                f"Записей: {len(proposal.transaction.writes)}; удалений: {len(proposal.transaction.removals)}.",
            ]
            if isinstance(proposal, AccountConfigPreview):
                actions = {
                    "untouched": "без действий",
                    "preserve": "сохранить существующую",
                    "create": "создать из шаблона",
                    "restore": "восстановить из выбранной копии",
                }
                lines.append("Localconfig: " + actions[proposal.plan.localconfig_action] + ".")
            controls.summary.setPlainText("\n".join(lines))
            changed = isinstance(proposal, AccountConfigPreview) or proposal.plan.changed
            controls.status.setText(
                "Предпросмотр готов. Применение изменит только рабочую копию."
                if changed
                else "Текст совпадает. Изменений нет; запись не требуется."
            )
        else:
            applied = response["value"]
            self.account_review = None
            self.autoexec_review = None
            for tab in (self.account, self.autoexec):
                tab.summary.clear()
            controls.status.setText(
                (
                    f"Копия сохранена. Резервная копия: {applied.backup_path}"
                    if applied.backup_path
                    else "Копия сохранена. Предыдущей папки для резервирования не было."
                )
                if applied.changed
                else "Изменений нет; запись не выполнялась."
            )
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
            self._inputs_edited()
            self.workspace_status.setText(
                "Операция прервана. Проверьте рабочую копию и повторите предпросмотр."
            )

    @Slot()
    def _closing_ui(self) -> None:
        self._refresh_controls()

    async def aclose(self) -> None:
        """Wait for actual file work; do not cancel a thread during tree replacement."""
        self._closing = True
        self.closing_started.emit()
        pending = self._future
        if pending is not None:
            await asyncio.shield(asyncio.wrap_future(pending))

    async def _close_shared(self) -> dict:
        await self.aclose()
        return {"config_files_closed": id(self)}

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self._refresh_controls()
        if self._owns_bridge:
            self.bridge.request_shutdown(self.aclose())
        else:
            self.bridge.submit(self._close_shared())

    @Slot()
    def _finish_close(self) -> None:
        self._closed = True
        self.close()
