import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtWidgets import QLineEdit

from fsm_panel.adapters.fakes import FakeLease, MemorySecretStore
from fsm_panel.adapters.settings_vault import SettingsVault
from fsm_panel.core.settings import SettingsStore
from fsm_panel.notifications import TelegramClient
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.notifications import NotificationPanel

TOKEN = "123456:Synthetic_test_token_abcdef"
TEXT = "private-test-message"


@pytest.fixture
def panel(qtbot, qapp):
    qapp.setQuitOnLastWindowClosed(False)
    calls = []
    clients = []

    async def handler(request):
        calls.append(request)
        message_id = 17 if "editMessage" in request.url.path else 42
        return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})

    owner = SimpleNamespace(
        bridge=AsyncBridge(),
        _closing=False,
        simulate=False,
        coordinator=SimpleNamespace(lease=FakeLease()),
        settings=SettingsStore.defaults(),
    )
    state = SimpleNamespace(handler=handler)

    def factory(token, **kwargs):
        client = TelegramClient(token, transport=httpx.MockTransport(state.handler), **kwargs)
        clients.append(client)
        return client

    widget = NotificationPanel(owner, client_factory=factory)
    # Own widget teardown until its asynchronous bridge cleanup has finished.
    widget.show()
    widget.token.setText(TOKEN)
    widget.destination.setText("-100123:4")
    widget.message.setPlainText(TEXT)
    yield widget, owner, state, calls, clients
    owner._closing = True
    owner.bridge.request_shutdown(widget.aclose())
    qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=10000)
    assert owner.bridge.shutdown_error is None
    assert all(client.is_closed for client in clients)
    widget.close()
    widget.deleteLater()


def done(qtbot, widget):
    qtbot.waitUntil(lambda: widget._future is None, timeout=5000)


@pytest.mark.parametrize("action", ["text", "photo", "edit_text", "edit_caption"])
def test_actions_use_single_request_and_close_client(panel, qtbot, tmp_path, action):
    widget, owner, _, calls, clients = panel
    photo = tmp_path / "synthetic.png"
    photo.write_bytes(b"synthetic-image-bytes")
    widget.photo_path.setText(str(photo))
    widget.message_id.setText("17")
    widget.action.setCurrentIndex(widget.action.findData(action))
    widget.send_button.click()
    widget._submit()  # Reentrant command must not duplicate the in-flight request.
    done(qtbot, widget)
    assert len(calls) == 1 and len(clients) == 1 and clients[0].is_closed
    assert owner.coordinator.lease.calls == ["notifications"]
    assert "подтверждён" in widget.status.text()
    assert ("17" if action.startswith("edit_") else "42") in widget.status.text()
    assert TEXT not in widget.status.text() and TOKEN not in widget.status.text()
    assert widget.send_button.isEnabled()


def test_simulator_blocks_direct_submit_and_button(panel):
    widget, owner, _, calls, clients = panel
    owner.simulate = True
    widget._refresh()
    widget._submit()
    assert not widget.send_button.isEnabled()
    assert widget._future is None and not calls and not clients


def test_revoked_lease_prevents_client_construction(panel, qtbot):
    widget, owner, _, calls, clients = panel
    owner.coordinator.lease.allowed = False
    widget.send_button.click()
    done(qtbot, widget)
    assert "допуск" in widget.status.text()
    assert not clients and not calls


@pytest.mark.parametrize("destination", ["1,2", "synthetic_bad_recipient", "1:bad topic"])
def test_invalid_recipient_never_contacts_transport(panel, qtbot, destination):
    widget, _, _, calls, _ = panel
    widget.destination.setText(destination)
    widget.send_button.click()
    done(qtbot, widget)
    assert not calls
    assert "invalid_input" in widget.status.text()
    assert destination not in widget.status.text()


def test_explicit_settings_loading_never_saves_form_edits(panel):
    widget, owner, _, _, _ = panel
    owner.settings.set("TG_BOT_API_KEY", TOKEN)
    owner.settings.set("TG_BOT_CHAT_ID", "1,2")
    owner.settings.set("PROXY_FOR_TG", "host:8080:user:secret")
    widget.load_button.click()
    assert widget.token.echoMode() == QLineEdit.EchoMode.Password
    assert widget.proxy.echoMode() == QLineEdit.EchoMode.Password
    assert widget.destination.text() == "1,2"
    assert widget.proxy.text() == "host:8080:user:secret"
    widget.token.setText("changed")
    assert owner.settings.get("TG_BOT_API_KEY") == TOKEN
    assert TOKEN not in widget.status.text()


@pytest.mark.parametrize("proxy", ["host:8080:user:secret", {"password": "private-proxy"}])
def test_imported_unsupported_proxy_never_falls_back_to_direct(panel, qtbot, proxy):
    widget, owner, _, calls, clients = panel
    owner.settings.set("TG_BOT_API_KEY", TOKEN)
    owner.settings.set("TG_BOT_CHAT_ID", "1")
    owner.settings.set("PROXY_FOR_TG", proxy)
    widget.load_button.click()
    widget.send_button.click()
    done(qtbot, widget)
    assert not clients and not calls
    assert "invalid_input" in widget.status.text()


@pytest.mark.parametrize("key", ["PROXY_FOR_TG", "proxy_for_tg"])
def test_proxy_credentials_are_redacted_and_sealed(key, tmp_path):
    document = SettingsStore.defaults()
    proxy = "http://synthetic_user:synthetic_password@example.invalid:8080"
    document.set(key, proxy)
    assert document.to_dict()[key] == "[redacted]"
    vault = SettingsVault(MemorySecretStore())
    path = tmp_path / "settings.vault.json"
    vault.save(document, path)
    assert "synthetic_password" not in path.read_text(encoding="utf-8")
    assert vault.load(path).get(key) == proxy


def test_unknown_delivery_never_retries_and_ignores_other_bridge_results(panel, qtbot):
    widget, _, state, calls, _ = panel

    async def lost(request):
        calls.append(request)
        raise httpx.ReadTimeout(TOKEN + TEXT)

    state.handler = lost
    widget.send_button.click()
    done(qtbot, widget)
    assert len(calls) == 1
    assert "Результат неизвестен" in widget.status.text()
    before = widget.status.text()
    widget._completed({"request": widget._request, "result": TOKEN})
    widget._failed(TOKEN)
    assert widget.status.text() == before
    assert TOKEN not in before and TEXT not in before


def test_cancel_waits_for_transport_cleanup_and_keeps_uncertain_result(panel, qtbot):
    widget, owner, state, calls, clients = panel
    entered = threading.Event()

    async def blocked(request):
        calls.append(request)
        entered.set()
        await asyncio.Event().wait()

    state.handler = blocked
    widget.send_button.click()
    qtbot.waitUntil(entered.is_set)
    owner.coordinator.lease.allowed = False
    widget.cancel_button.click()
    widget._cancel()  # repeated cancellation cannot detach cleanup
    done(qtbot, widget)
    assert len(calls) == 1 and clients[0].is_closed
    assert "Результат неизвестен" in widget.status.text()
    assert not widget.cancel_button.isEnabled()


def test_cancel_during_admission_never_constructs_client(panel, qtbot):
    widget, owner, _, calls, clients = panel
    entered = threading.Event()

    async def admit(account_id):
        entered.set()
        await asyncio.Event().wait()

    owner.coordinator.lease.admit = admit
    widget.send_button.click()
    qtbot.waitUntil(entered.is_set)
    widget.cancel_button.click()
    done(qtbot, widget)
    assert not calls and not clients
    assert "не отправлено" in widget.status.text()


def test_close_joins_inflight_work_without_external_messages(panel, qtbot):
    widget, owner, state, calls, clients = panel
    entered = threading.Event()

    async def blocked(request):
        calls.append(request)
        entered.set()
        await asyncio.Event().wait()

    state.handler = blocked
    widget.send_button.click()
    qtbot.waitUntil(entered.is_set)
    closed = owner.bridge.submit(widget.aclose())
    qtbot.waitUntil(closed.done)
    assert closed.result() is None
    assert clients[0].is_closed and len(calls) == 1
    assert not widget.send_button.isEnabled()


def test_invalid_edit_id_is_local_and_private(panel):
    widget, _, _, calls, clients = panel
    widget.action.setCurrentIndex(widget.action.findData("edit_text"))
    widget.message_id.setText(TOKEN)
    widget.send_button.click()
    assert widget._future is None and not calls and not clients
    assert TOKEN not in widget.status.text()
