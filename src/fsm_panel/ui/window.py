"""Operator interface; availability and simulation are always visible."""

import json
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fsm_panel.adapters.settings_vault import SettingsVault
from fsm_panel.core import AccountRepository, Coordinator, SettingsStore, import_accounts
from fsm_panel.core.contracts import SecretStore
from fsm_panel.core.settings import SECRET_KEYS, SETTINGS_TYPES, SettingsDocument

from .bridge import AsyncBridge


class MainWindow(QMainWindow):
    def __init__(
        self,
        repository: AccountRepository,
        coordinator: Coordinator,
        secrets: SecretStore,
        data_dir: Path,
        *,
        simulate: bool = False,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.coordinator = coordinator
        self.secrets = secrets
        self.data_dir = data_dir
        self.simulate = simulate
        self.ready = False
        self._closing = False
        self._closed = False
        self.license_runtime = None
        self.settings_vault = SettingsVault(secrets)
        self.settings_path = data_dir / "settings.vault.json"
        self.settings = (
            self.settings_vault.load(self.settings_path)
            if self.settings_path.exists()
            else SettingsStore.defaults()
        )
        self.bridge = AsyncBridge()
        self.bridge.failed.connect(self._failed)
        self.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self.bridge.domain_event.connect(self._event)
        self.bridge.shutdown_finished.connect(self._finish_close)
        coordinator.subscribe(self.bridge.domain_event.emit)
        self.setWindowTitle("FSM Panel Open — исследовательская версия")
        self.resize(1150, 720)
        central = QWidget()
        layout = QVBoxLayout(central)
        self.banner = QLabel(
            "СИМУЛЯТОР · Steam и CS2 не запускаются · результаты не подтверждают совместимость"
            if simulate
            else "РЕАЛЬНЫЙ РЕЖИМ · игровые сценарии недоступны до завершения исследования"
        )
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            "padding: 12px; background: #493b15; color: #ffdc80; font-weight: bold"
        )
        layout.addWidget(self.banner)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_accounts()
        self._build_settings()
        self._build_license()
        self.bridge.license_state.connect(self.license_status.setText)
        self._build_compatibility()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.tabs.addTab(self.log, "Журнал")
        self.setCentralWidget(central)
        self.refresh_accounts()
        self.start_button.setEnabled(False)
        self._startup = self.bridge.submit(self.coordinator.recover())

    def _build_accounts(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.import_button = QPushButton("Импорт аккаунтов")
        self.import_button.clicked.connect(self._import_dialog)
        actions.addWidget(self.import_button)
        if self.simulate:
            sample = QPushButton("Добавить тестовый аккаунт")
            sample.clicked.connect(self.add_sample)
            actions.addWidget(sample)
        self.start_button = QPushButton("Запустить выбранные")
        self.start_button.clicked.connect(self.start_selected)
        self.stop_button = QPushButton("Остановить выбранные")
        self.stop_button.clicked.connect(self.stop_selected)
        pause = QPushButton("Пауза")
        pause.clicked.connect(lambda: self._selected_command("pause"))
        resume = QPushButton("Продолжить")
        resume.clicked.connect(lambda: self._selected_command("resume"))
        for button in (self.start_button, self.stop_button, pause, resume):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Аккаунт", "Имя", "Состояние", "Причина / ошибка"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.tabs.addTab(page, "Аккаунты")

    def _build_settings(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        notice = QLabel(
            "49 полей исходного формата. Их импорт не означает, что игровые функции уже восстановлены."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        buttons = QHBoxLayout()
        load = QPushButton("Импорт JSON")
        load.clicked.connect(self._load_settings_dialog)
        save = QPushButton("Сохранить")
        save.clicked.connect(self.save_settings)
        export = QPushButton("Экспорт без секретов")
        export.clicked.connect(self._export_settings)
        for button in (load, save, export):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_page = QWidget()
        form = QFormLayout(form_page)
        self.setting_fields: dict[str, QCheckBox | QLineEdit] = {}
        for key, value_type in SETTINGS_TYPES.items():
            field = QCheckBox() if value_type is bool else QLineEdit()
            field.setObjectName(key)
            if isinstance(field, QLineEdit) and key in SECRET_KEYS:
                field.setEchoMode(QLineEdit.EchoMode.Password)
            self.setting_fields[key] = field
            form.addRow(key, field)
        scroll.setWidget(form_page)
        layout.addWidget(scroll)
        self.tabs.addTab(page, "Настройки")
        self._refresh_settings()

    def _build_license(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.license_status = QLabel(
            "Симулятор: тестовый допуск" if self.simulate else "Лицензия не активирована"
        )
        self.license_status.setWordWrap(True)
        layout.addWidget(self.license_status)
        layout.addWidget(
            QLabel(
                "Оплата выполняется во внешнем приложении. Здесь проверяются только права доступа."
            )
        )
        self.license_form = QFormLayout()
        self.license_url = QLineEdit("https://")
        self.license_key = QLineEdit()
        self.license_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.license_form.addRow("Сервер лицензий", self.license_url)
        self.license_form.addRow("Ключ активации", self.license_key)
        layout.addLayout(self.license_form)
        self.activate_button = QPushButton("Активировать")
        self.activate_button.setEnabled(False)
        self.activate_button.setToolTip("Проверенный ключ сервера должен быть настроен при запуске")
        layout.addWidget(self.activate_button)
        layout.addStretch()
        self.tabs.addTab(page, "Лицензия")

    def _build_compatibility(self) -> None:
        page = QPlainTextEdit()
        page.setReadOnly(True)
        page.setPlainText(
            "Статус: исследовательская версия, функциональный паритет не подтверждён.\n\n"
            "Доступно: локальные аккаунты, импорт/экспорт настроек, журнал и симулятор.\n"
            "Сервер лицензирования проверяется отдельно контрактными тестами.\n\n"
            "Пока не восстановлены: реальный запуск Steam/CS2, sandbox, лобби, поиск, "
            "autoaccept, anti-AFK, reconnect, смена партий и статистика дропа.\n\n"
            "Причина: нативная логика оригинала исследуется; рабочие функции оригинала "
            "закрыты лицензией.\n\nМатрица доказательств: docs/feature-matrix.json.\n"
            "Тесты симулятора не заменяют стенд с 50 реальными клиентами."
        )
        self.tabs.addTab(page, "Совместимость")

    def add_sample(self) -> None:
        number = len(self.repository.list_accounts()) + 1
        self.repository.add_account(f"demo_{number}", display_name=f"Тест {number}")
        self.refresh_accounts()

    def attach_license(self, client, base_url: str) -> None:
        from fsm_panel.application import LicenseRuntime

        self.license_runtime = LicenseRuntime(
            client, self.coordinator, self.bridge.license_state.emit
        )
        self.license_url.setText(base_url)
        self.license_url.setReadOnly(True)
        self.activate_button.setEnabled(True)
        self.activate_button.setToolTip(
            "Сервер и ключ проверки закреплены в локальной конфигурации"
        )
        self.activate_button.clicked.connect(self._activate)
        self.bridge.submit(self.license_runtime.run())

    def _activate(self) -> None:
        if self.license_runtime is None:
            return
        key = self.license_key.text()
        self.license_key.clear()
        self.bridge.submit(self.license_runtime.activate(key))

    def selected_ids(self) -> list[str]:
        return [
            str(item.data(Qt.ItemDataRole.UserRole))
            for row in sorted({index.row() for index in self.table.selectedIndexes()})
            if (item := self.table.item(row, 0)) is not None
        ]

    def start_selected(self) -> None:
        if not self.ready or self._closing:
            return
        self.bridge.submit(self.coordinator.start_batch(self.selected_ids()))

    def stop_selected(self) -> None:
        self._selected_command("stop")

    def _selected_command(self, name: str) -> None:
        for account_id in self.selected_ids():
            self.bridge.submit(getattr(self.coordinator, name)(account_id))

    def import_text(self, text: str, format: str = "lines") -> int:
        result = import_accounts(text, self.repository, self.secrets, format=format)
        self.refresh_accounts()
        return result.count

    def _import_dialog(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Импорт login:password или CSV", "", "Text (*.txt *.csv)"
        )
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("import_too_large")
            count = self.import_text(
                path.read_text(encoding="utf-8-sig"),
                "csv" if path.suffix.lower() == ".csv" else "lines",
            )
            self.statusBar().showMessage(f"Импортировано: {count}")
        except Exception:
            self._failed("Импорт не выполнен: проверьте формат и повторяющиеся аккаунты")

    @Slot()
    def refresh_accounts(self) -> None:
        selected = set(self.selected_ids())
        accounts = self.repository.list_accounts()
        self.table.setRowCount(len(accounts))
        for row, account in enumerate(accounts):
            values = [
                account.login,
                account.display_name,
                account.state.value,
                account.error_code or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, account.id)
                self.table.setItem(row, column, item)
                if account.id in selected:
                    item.setSelected(True)
        self.statusBar().showMessage(f"Аккаунтов: {len(accounts)}")

    def _refresh_settings(self) -> None:
        defaults = SettingsStore.defaults()
        for key, field in self.setting_fields.items():
            value = self.settings.get(key, defaults.get(key))
            if isinstance(field, QCheckBox):
                field.setChecked(value)
            else:
                field.setText(str(value))

    def save_settings(self) -> None:
        candidate = SettingsDocument(self.settings.to_dict(include_secrets=True))
        try:
            for key, field in self.setting_fields.items():
                value = (
                    field.isChecked()
                    if isinstance(field, QCheckBox)
                    else SETTINGS_TYPES[key](field.text())
                )
                candidate.set(key, value)
            self.settings_vault.save(candidate, self.settings_path)
        except Exception:
            self._failed("Настройки не сохранены: проверьте типы и доступ к файлу")
            return
        self.settings = candidate
        self.statusBar().showMessage("Настройки сохранены; предыдущая версия сохранена в .bak")

    def _load_settings_dialog(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Импорт настроек", "", "JSON (*.json)")
        if filename:
            try:
                self.settings = SettingsStore.load(filename)
                self._refresh_settings()
            except Exception:
                self._failed("Некорректный файл настроек")

    def _export_settings(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "Экспорт без секретов", "settings-redacted.json", "JSON (*.json)"
        )
        if filename:
            try:
                Path(filename).write_text(
                    json.dumps(self.settings.redacted(), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                self._failed("Экспорт не выполнен")

    @Slot(str)
    def _failed(self, code: str) -> None:
        self.statusBar().showMessage(code)
        if hasattr(self, "log"):
            self.log.appendPlainText(code)

    @Slot(object)
    def _completed(self, result: object) -> None:
        if not self.ready and self._startup.done() and self._startup.exception() is None:
            self.ready = True
            self.start_button.setEnabled(True)
        self.refresh_accounts()

    @Slot(object)
    def _event(self, event: object) -> None:
        self.refresh_accounts()
        self.log.appendPlainText(
            f"{getattr(event, 'sequence', '')} {getattr(event, 'kind', '')} {getattr(event, 'code', '') or ''}"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self.tabs.setEnabled(False)
        self.statusBar().showMessage("Остановка задач и сохранение состояния…")

        async def cleanup() -> None:
            try:
                if self.license_runtime:
                    await self.license_runtime.close()
            finally:
                await self.coordinator.close()

        self.bridge.request_shutdown(cleanup())

    @Slot()
    def _finish_close(self) -> None:
        self._closed = True
        self.close()
