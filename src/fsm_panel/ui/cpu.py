"""Operator controls for registered processes, with explicit unverified delivery."""

import asyncio

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fsm_panel.adapters.bes_plan import LEGACY_BES_FALLBACK_PERCENT


class CpuPanel(QWidget):
    def __init__(self, owner) -> None:
        super().__init__()
        self.owner = owner
        self._future = None
        layout = QVBoxLayout(self)
        notice = QLabel(
            "BES · управление только процессами, зарегистрированными этой панелью. "
            "Команда ограничивает долю CPU, а не задаёт целевую загрузку. "
            "Фактическое ограничение ещё предстоит проверить на стенде. "
            "Симулятор не создаёт игровых процессов для BES."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.executable = QLineEdit()
        self.browse_button = QPushButton("Выбрать BES.exe…")
        self.browse_button.clicked.connect(self._browse)
        paths = QHBoxLayout()
        paths.addWidget(self.executable)
        paths.addWidget(self.browse_button)
        layout.addLayout(paths)
        form = QFormLayout()
        self.target = QComboBox()
        self.percent = QSpinBox()
        self.percent.setRange(1, 99)
        self.percent.setValue(LEGACY_BES_FALLBACK_PERCENT)
        self.settings_button = QPushButton("Прочитать из настроек")
        self.settings_button.clicked.connect(self.load_percent)
        percent_row = QHBoxLayout()
        percent_row.addWidget(self.percent)
        percent_row.addWidget(self.settings_button)
        form.addRow("Принадлежащий панели процесс", self.target)
        form.addRow("Уменьшение доступного CPU, %", percent_row)
        layout.addLayout(form)
        actions = QGridLayout()
        self.refresh_button = QPushButton("Обновить процессы")
        self.limit_button = QPushButton("Применить ограничение")
        self.stop_button = QPushButton("Снять ограничения выбранного аккаунта")
        self.recover_button = QPushButton("Завершить все собственные сессии BES")
        self.refresh_button.clicked.connect(self.refresh)
        self.limit_button.clicked.connect(lambda: self._submit("limit"))
        self.stop_button.clicked.connect(lambda: self._submit("stop"))
        self.recover_button.clicked.connect(lambda: self._submit("recover"))
        for index, button in enumerate(
            (
                self.refresh_button,
                self.limit_button,
                self.stop_button,
                self.recover_button,
            )
        ):
            actions.addWidget(button, index // 2, index % 2)
        layout.addLayout(actions)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMaximumBlockCount(300)
        layout.addWidget(self.result)
        self.load_percent()
        owner.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        owner.bridge.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.refresh()

    def load_percent(self) -> None:
        value = self.owner.settings.get("BES_REDUCTION_PERCENT")
        if type(value) is int and 1 <= value <= 99:
            self.percent.setValue(value)
        else:
            self.result.appendPlainText(
                "BES_REDUCTION_PERCENT должен быть целым числом от 1 до 99. "
                "Текущее значение формы сохранено."
            )

    def _browse(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "BES 1.7.9", "", "Executable (*.exe)")
        if filename:
            self.executable.setText(filename)

    def refresh(self) -> None:
        selection = self.target.currentData()
        self.target.clear()
        for record in self.owner.repository.owned_processes():
            self.target.addItem(f"PID {record['pid']} · {record['account_id']}", record["token"])
        if selection is not None:
            self.target.setCurrentIndex(self.target.findData(selection))
        if self.target.currentIndex() < 0 and self.target.count():
            self.target.setCurrentIndex(0)
        self._busy(self._future is not None)

    def _busy(self, busy: bool):
        for control in (
            self.executable,
            self.browse_button,
            self.target,
            self.percent,
            self.settings_button,
            self.refresh_button,
            self.stop_button,
            self.recover_button,
        ):
            control.setEnabled(not busy)
        self.limit_button.setEnabled(
            not busy and self.target.count() > 0 and self.owner.ready and not self.owner.simulate
        )

    def _submit(self, action: str) -> None:
        if self.owner._closing or self._future is not None:
            return
        if action == "limit" and (not self.owner.ready or self.owner.simulate):
            return
        accounts = self.owner.selected_ids() if action == "stop" else []
        if action == "stop" and len(accounts) != 1:
            self.result.appendPlainText("Выберите один аккаунт на вкладке «Аккаунты».")
            return
        self._busy(True)
        self._future = self.owner.bridge.submit(
            self._perform(
                action,
                self.target.currentData(),
                self.percent.value(),
                self.executable.text(),
                accounts[0] if accounts else None,
            )
        )

    async def _perform(self, action, token, percent, executable, account_id) -> dict:
        response: dict = {"cpu_panel": id(self)}
        try:
            if action == "limit":
                session = await self.owner.cpu_runtime.limit(token, percent, executable)
                response["message"] = (
                    f"Состояние сессии BES: {session.state}. "
                    "Доставка команды не подтверждает фактическое ограничение CPU."
                )
            elif action == "stop":
                await self.owner.cpu_runtime.stop(account_id)
                response["message"] = "Собственные сессии BES аккаунта завершены."
            else:
                await self.owner.cpu_runtime.recover()
                if not self.owner.ready:
                    response.update(await self.owner.recover_startup())
                response["message"] = "Восстановление собственных сессий BES завершено."
        except Exception as error:
            response["message"] = (
                f"Операция не завершена: {getattr(error, 'code', type(error).__name__)}."
            )
        return response

    @Slot(object)
    def _completed(self, result: object) -> None:
        if isinstance(result, dict) and result.get("cpu_panel") == id(self):
            self._future = None
            self.result.appendPlainText(result["message"])
            self.refresh()

    @Slot(str)
    def _failed(self, code: str) -> None:
        if (
            self._future is not None
            and self._future.done()
            and (self._future.cancelled() or self._future.exception() is not None)
        ):
            self._future = None
            self.result.appendPlainText(
                "Операция прервана; требуется сверка собственных сессий BES."
            )
            self.refresh()

    async def aclose(self) -> None:
        pending = self._future
        if pending is not None:
            await asyncio.wrap_future(pending)
