"""Explicit synthetic inventory/trade slice; live operations are not exposed yet."""

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget


class TradePanel(QWidget):
    def __init__(self, owner) -> None:
        super().__init__()
        self.owner = owner
        self.worker = None
        self.service = None
        self.operation_id: str | None = None
        self.account_id: str | None = None
        self._future = None
        layout = QVBoxLayout(self)
        label = QLabel(
            "СИМУЛЯТОР ОБМЕНОВ · синтетический инвентарь и локальный журнал. "
            "Выберите один аккаунт на вкладке «Аккаунты». Steam не вызывается."
            if owner.simulate
            else "Реальные обмены в интерфейсе недоступны до проверки Steam-адаптера на стенде. "
            "Контракт и тестовый worker включены в исходники."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        self.inventory_button = QPushButton("Тестовый инвентарь")
        self.send_button = QPushButton("Создать тестовый обмен")
        self.reconcile_button = QPushButton("Сверить последний обмен")
        self.cancel_button = QPushButton("Отменить ожидание")
        self.inventory_button.clicked.connect(lambda: self._submit("inventory"))
        self.send_button.clicked.connect(lambda: self._submit("send"))
        self.reconcile_button.clicked.connect(lambda: self._submit("reconcile"))
        self.cancel_button.clicked.connect(self._cancel)
        for button in (
            self.inventory_button,
            self.send_button,
            self.reconcile_button,
            self.cancel_button,
        ):
            row.addWidget(button)
        layout.addLayout(row)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMaximumBlockCount(1000)
        layout.addWidget(self.result)
        self._set_busy(False)
        owner.bridge.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        owner.bridge.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)

    def _set_busy(self, busy: bool) -> None:
        allowed = self.owner.simulate and not busy
        self.inventory_button.setEnabled(allowed)
        self.send_button.setEnabled(allowed)
        self.reconcile_button.setEnabled(allowed and self.operation_id is not None)
        self.cancel_button.setEnabled(busy)

    async def _get_service(self):
        if self.service is None:
            from fsm_panel.trading import NodeWorkerClient, TradeService

            if getattr(sys, "frozen", False):
                root = Path(sys.executable).parent
                node = root / "worker/node.exe"
                script = root / "worker/dist/main.js"
            else:
                root = Path(__file__).resolve().parents[3]
                node_path = shutil.which("node")
                if not node_path:
                    raise RuntimeError("node_runtime_missing")
                node = Path(node_path)
                script = root / "worker/dist/main.js"
                if not script.is_file():
                    script = root / "worker/src/main.ts"
            if not script.is_file():
                raise RuntimeError("worker_not_built")
            scenario = self.owner.data_dir / "builtin-simulation.json"
            scenario.write_text(
                json.dumps(
                    {
                        "pages": {
                            "730/2": [
                                {
                                    "assets": [
                                        {
                                            "app_id": 730,
                                            "context_id": "2",
                                            "asset_id": "1001",
                                            "amount": 1,
                                        }
                                    ]
                                }
                            ]
                        },
                        "send_status": "sent",
                    }
                ),
                encoding="utf-8",
            )
            self.worker = NodeWorkerClient(
                [
                    str(node),
                    str(script),
                    "--state",
                    str(self.owner.data_dir / "trades-simulation.sqlite"),
                    "--mode",
                    "simulation",
                    "--scenario",
                    str(scenario),
                ]
            )

            async def synthetic_admission(account_id: str) -> int:
                return int(time.time() * 1000) + 55_000

            self.service = TradeService(self.owner.repository, self.worker, synthetic_admission)
        return self.service

    def _submit(self, action: str) -> None:
        if not self.owner.simulate or self.owner._closing:
            return
        selected = self.owner.selected_ids()
        if action == "reconcile":
            account_id = self.account_id
        else:
            if len(selected) != 1:
                self.result.appendPlainText("Выберите ровно один аккаунт.")
                return
            account_id = selected[0]
        if action == "send":
            self.account_id = account_id
            self.operation_id = str(uuid.uuid4())
            self.result.appendPlainText(f"Операция: {self.operation_id}")
        self._set_busy(True)
        self._future = self.owner.bridge.submit(
            self._perform(action, account_id, self.operation_id)
        )

    async def _perform(self, action: str, account_id: str | None, operation_id: str | None) -> dict:
        from fsm_panel.trading import SteamCredentials

        service = await self._get_service()
        if account_id is None:
            raise RuntimeError("trade_account_missing")
        credentials = SteamCredentials(
            account_name="synthetic",
            password="synthetic-password",
            shared_secret="c3ludGhldGlj",
            identity_secret="c3ludGhldGlj",
        )
        if action == "inventory":
            result = await service.inventory(account_id, credentials)
            message = f"Тестовый инвентарь: {len(result.assets)} предметов; ошибок: {len(result.failures)}"
        else:
            if operation_id is None:
                raise RuntimeError("trade_operation_missing")
            if action == "send":
                result = await service.send(
                    account_id,
                    operation_id,
                    "https://steamcommunity.com/tradeoffer/new/?partner=123&token=synthetic",
                    credentials,
                )
            else:
                result = await service.reconcile(account_id, operation_id, credentials)
            message = (
                f"{result.operation_id}: {result.state.value}; offer_id={result.offer_id or '—'}"
            )
        return {"trade_panel": id(self), "message": message}

    @Slot(object)
    def _completed(self, result: object) -> None:
        if isinstance(result, dict) and result.get("trade_panel") == id(self):
            self.result.appendPlainText(result["message"])
            self._set_busy(False)

    @Slot(str)
    def _failed(self, code: str) -> None:
        if self._future is not None and self._future.done():
            self.result.appendPlainText(f"Операция не завершена: {code}. Сверьте последний обмен.")
            self._set_busy(False)

    def _cancel(self) -> None:
        if self._future is not None:
            self._future.cancel()

    async def aclose(self) -> None:
        if self.worker:
            await self.worker.close()
