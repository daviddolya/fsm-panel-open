import time

import pytest
from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore, RealClock
from fsm_panel.core import AccountRepository, Coordinator, SettingsStore
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from fsm_panel.ui.window import MainWindow


@pytest.fixture
def window(qtbot, tmp_path):
    repo = AccountRepository(tmp_path / "accounts.db")
    coordinator = Coordinator(repo, game=FakeGame(), lease=FakeLease(), clock=RealClock())
    widget = MainWindow(repo, coordinator, MemorySecretStore(), tmp_path, simulate=True)
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    widget.close()
    repo.close()


def test_simulator_is_explicit_and_accounts_run_stop_without_blocking(qtbot, window):
    assert "СИМУЛЯТОР" in window.banner.text()
    window.add_sample()
    window.table.selectRow(0)
    qtbot.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.table.item(0, 2).text() == "running", timeout=3000)
    window.table.selectRow(0)
    started = time.monotonic()
    qtbot.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
    assert time.monotonic() - started < 1
    qtbot.waitUntil(lambda: window.table.item(0, 2).text() == "stopped", timeout=3000)


def test_import_stores_metadata_without_showing_password(window):
    assert window.import_text("synthetic:secret:with:colon") == 1
    texts = [window.table.item(0, column).text() for column in range(4)]
    assert "secret" not in " ".join(texts)
    assert "secret" not in window.log.toPlainText()


def test_settings_roundtrip_preserves_unknown_and_masks_secrets(window):
    window.settings.set("new_extension", {"flag": True})
    secret = window.setting_fields["TG_BOT_API_KEY"]
    assert isinstance(secret, QLineEdit)
    assert secret.echoMode() == QLineEdit.EchoMode.Password
    secret.setText("synthetic-test-token")
    window.save_settings()
    loaded = SettingsStore.load(window.settings_path)
    assert loaded.get("TG_BOT_API_KEY") == "synthetic-test-token"
    assert loaded.get("new_extension") == {"flag": True}
    assert "synthetic-test-token" not in window.log.toPlainText()


def test_invalid_field_does_not_replace_saved_settings(window):
    window.save_settings()
    before = window.settings_path.read_bytes()
    field = window.setting_fields["ACCOUNT_LOGIN_ATTEMPTS"]
    assert isinstance(field, QLineEdit)
    field.setText("invalid")
    window.save_settings()
    assert window.settings_path.read_bytes() == before


def test_fifty_rows_and_status_events_remain_usable(qtbot, window):
    for _ in range(50):
        window.add_sample()
    assert window.table.rowCount() == 50
    window.table.selectAll()
    window.start_selected()
    qtbot.waitUntil(
        lambda: all(a.state.value == "running" for a in window.repository.list_accounts()),
        timeout=10000,
    )
    window.table.selectAll()
    window.stop_selected()
    qtbot.waitUntil(
        lambda: all(a.state.value == "stopped" for a in window.repository.list_accounts()),
        timeout=10000,
    )
