"""Independent offline review of manual notification ownership and UI boundaries."""

import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtCore import QThread

from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.adapters.settings_vault import SettingsVault
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.core.models import Admission, ValidationError
from fsm_panel.core.settings import SettingsStore
from fsm_panel.notifications import DeliveryState, TelegramClient
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.notifications import NotificationPanel
from fsm_panel.ui.window import MainWindow

TOKEN = "543210:Independent_synthetic_token"
PRIVATE = "private_synthetic_ui_value"
PROXY = "https://synthetic_user:synthetic_password@proxy.invalid:8443"


@pytest.fixture
def case(qtbot, qapp):
    qapp.setQuitOnLastWindowClosed(False)
    state = SimpleNamespace(requests=[], clients=[], releases=[], completed=[], failed=[])

    async def handler(request):
        state.requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 321}})

    state.handler = handler
    state.transport_factory = httpx.MockTransport

    def factory(token, **kwargs):
        client = TelegramClient(token, transport=state.transport_factory(state.handler), **kwargs)
        state.clients.append(client)
        return client

    owner = SimpleNamespace(
        bridge=AsyncBridge(),
        coordinator=SimpleNamespace(lease=FakeLease()),
        settings=SettingsStore.defaults(),
        simulate=False,
        _closing=False,
    )
    widget = NotificationPanel(owner, client_factory=factory)
    owner.bridge.completed.connect(state.completed.append)
    owner.bridge.failed.connect(state.failed.append)
    widget.token.setText(TOKEN)
    widget.destination.setText("-12345:9")
    widget.message.setPlainText(PRIVATE)
    widget.show()
    yield widget, owner, state
    for release in state.releases:
        owner.bridge.loop.call_soon_threadsafe(release.set)
    owner._closing = True
    owner.bridge.request_shutdown(widget.aclose())
    qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=5000)
    assert owner.bridge.shutdown_error is None
    widget.close()
    widget.deleteLater()


def _complete(qtbot, widget):
    qtbot.waitUntil(lambda: widget._future is None, timeout=3000)


def _turn(qtbot, loop):
    """Wait for callbacks and the tasks they wake, without a timing assertion."""
    progressed = threading.Event()
    loop.call_soon_threadsafe(lambda: loop.call_soon(progressed.set))
    qtbot.waitUntil(progressed.is_set, timeout=2000)


def _block_transport_close(state):
    entered = threading.Event()
    release = asyncio.Event()
    state.releases.append(release)
    closed = threading.Event()

    class Transport(httpx.MockTransport):
        async def aclose(self):
            entered.set()
            await release.wait()
            closed.set()

    state.transport_factory = Transport
    return entered, release, closed


def test_cancel_before_worker_coroutine_starts_never_admits_or_constructs(case, qtbot):
    widget, owner, state = case
    entered, release = threading.Event(), threading.Event()

    def block_worker():
        entered.set()
        assert release.wait(3)

    owner.bridge.loop.call_soon_threadsafe(block_worker)
    qtbot.waitUntil(entered.is_set)
    try:
        widget.send_button.click()
        job, future = widget._job, widget._future
        widget.cancel_button.click()
        assert job.cancelled.is_set() and job.task is None
    finally:
        release.set()
    _complete(qtbot, widget)
    response = future.result()
    assert response["result"].state == DeliveryState.NOT_SENT
    assert response["result"].code == "cancelled"
    assert job.task is None
    assert not owner.coordinator.lease.calls and not state.clients and not state.requests


def test_cancel_during_client_close_retains_receipt_and_joins_transport(case, qtbot):
    widget, owner, state = case
    entered, release, closed = _block_transport_close(state)
    widget.send_button.click()
    job, future = widget._job, widget._future
    qtbot.waitUntil(entered.is_set)
    owner.coordinator.lease.allowed = False
    widget.cancel_button.click()
    _turn(qtbot, owner.bridge.loop)
    assert not future.done() and not closed.is_set()
    assert not widget.send_button.isEnabled() and widget._job is job
    owner.bridge.loop.call_soon_threadsafe(release.set)
    _complete(qtbot, widget)
    response = future.result()
    assert response["result"].state == DeliveryState.SENT
    assert response["result"].message_id == 321 and response["status"] is None
    assert closed.is_set() and state.clients[0].is_closed and job.task is None
    assert "321" in widget.status.text() and "неизвестен" not in widget.status.text()
    assert owner.coordinator.lease.calls == ["notifications"]
    assert not state.failed


def test_cancelled_panel_close_waiter_cannot_return_before_owned_cleanup(case, qtbot):
    widget, owner, state = case
    entered, release, closed = _block_transport_close(state)
    widget.send_button.click()
    qtbot.waitUntil(entered.is_set)
    waiter = SimpleNamespace(task=None, cancellation=False)
    finished = threading.Event()

    async def supervise_close():
        waiter.task = asyncio.create_task(widget.aclose())
        try:
            await waiter.task
        except asyncio.CancelledError:
            waiter.cancellation = True
        finally:
            finished.set()

    supervisor = owner.bridge.submit(supervise_close())
    qtbot.waitUntil(lambda: waiter.task is not None and widget._closing)
    owner.bridge.loop.call_soon_threadsafe(waiter.task.cancel)
    _turn(qtbot, owner.bridge.loop)
    owner.bridge.loop.call_soon_threadsafe(waiter.task.cancel)
    _turn(qtbot, owner.bridge.loop)
    assert not finished.is_set() and not supervisor.done() and not closed.is_set()
    owner.bridge.loop.call_soon_threadsafe(release.set)
    qtbot.waitUntil(finished.is_set)
    assert supervisor.result() is None and waiter.cancellation
    assert closed.is_set() and state.clients[0].is_closed
    assert owner.coordinator.lease.calls == ["notifications"]
    _complete(qtbot, widget)
    assert not widget.send_button.isEnabled()


def test_transport_close_error_preserves_delivery_but_blocks_further_commands(case, qtbot, caplog):
    widget, _, state = case
    caplog.set_level(logging.DEBUG)

    class FailingClose(httpx.MockTransport):
        async def aclose(self):
            raise RuntimeError(PRIVATE + TOKEN + PROXY)

    state.transport_factory = FailingClose
    widget.send_button.click()
    job, future = widget._job, widget._future
    _complete(qtbot, widget)
    response = future.result()
    assert response["result"].state == "sent" and response["result"].message_id == 321
    assert response["status"] == "cleanup_failed" and job.task is None
    assert not widget.send_button.isEnabled()
    widget._submit()
    assert len(state.requests) == 1 and len(state.clients) == 1
    for secret in (TOKEN, PRIVATE, PROXY):
        assert secret not in repr(response) + widget.status.text() + caplog.text


@pytest.mark.parametrize("phase", ["admission", "constructor"])
def test_private_failures_before_dispatch_are_not_sent_and_never_leak(case, qtbot, caplog, phase):
    widget, owner, state = case
    caplog.set_level(logging.DEBUG)

    async def failed_admission(operation):
        raise RuntimeError(TOKEN + PRIVATE + PROXY)

    def failed_factory(*args, **kwargs):
        raise RuntimeError(TOKEN + PRIVATE + PROXY)

    if phase == "admission":
        owner.coordinator.lease.admit = failed_admission
    else:
        widget._client_factory = failed_factory
    widget.send_button.click()
    job, future = widget._job, widget._future
    _complete(qtbot, widget)
    assert future.result()["result"].state == "not_sent"
    assert not state.requests and not state.clients and job.task is None
    assert not state.failed and widget.send_button.isEnabled()
    for secret in (TOKEN, PRIVATE, PROXY):
        assert secret not in widget.status.text() + caplog.text + repr(future.result())


@pytest.mark.parametrize("admission", [None, True, {"allowed": True}, Admission(1, PRIVATE)])
def test_admission_requires_a_typed_boolean_allow(case, qtbot, admission):
    widget, owner, state = case

    async def malformed(operation):
        return admission

    owner.coordinator.lease.admit = malformed
    widget.send_button.click()
    future = widget._future
    _complete(qtbot, widget)
    assert future.result()["status"] == "license_denied"
    assert not state.requests and not state.clients
    assert PRIVATE not in widget.status.text()


@pytest.mark.parametrize("configured", [False, 0, [], {}, " ", "socks5://host:1080"])
def test_bad_imported_proxy_cannot_become_direct(case, qtbot, configured):
    widget, owner, state = case
    owner.settings.set("TG_BOT_API_KEY", TOKEN)
    owner.settings.set("TG_BOT_CHAT_ID", "123")
    owner.settings.set("PROXY_FOR_TG", configured)
    widget.load_button.click()
    assert widget.proxy.text()
    widget.send_button.click()
    future = widget._future
    _complete(qtbot, widget)
    assert future.result()["result"].state == "not_sent"
    assert future.result()["result"].code == "invalid_input"
    assert not state.requests and not state.clients


def test_valid_imported_proxy_reaches_transport_without_direct_fallback(case, qtbot, monkeypatch):
    widget, owner, state = case
    construction = []

    def network_factory(**kwargs):
        construction.append(kwargs)
        return httpx.MockTransport(state.handler)

    def client_factory(token, **kwargs):
        client = TelegramClient(token, **kwargs)
        state.clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", network_factory)
    widget._client_factory = client_factory
    owner.settings.set("TG_BOT_API_KEY", TOKEN)
    owner.settings.set("TG_BOT_CHAT_ID", "123")
    owner.settings.set("PROXY_FOR_TG", PROXY)
    widget.load_button.click()
    widget.send_button.click()
    _complete(qtbot, widget)
    assert len(construction) == 1 and construction[0]["proxy"] == PROXY
    assert construction[0]["verify"] is True and construction[0]["trust_env"] is False
    assert len(state.requests) == 1 and state.clients[0].is_closed
    assert "synthetic_password" not in widget.status.text()


def test_stale_completion_and_other_bridge_error_cannot_unlock_active_request(case, qtbot):
    widget, owner, state = case
    widget.send_button.click()
    previous = widget._future
    _complete(qtbot, widget)
    entered = threading.Event()

    async def blocked(request):
        state.requests.append(request)
        entered.set()
        await asyncio.Event().wait()

    state.handler = blocked
    widget.send_button.click()
    active, job = widget._future, widget._job
    qtbot.waitUntil(entered.is_set)
    widget._completed(previous.result())
    widget._failed(PRIVATE)
    assert widget._future is active and widget._job is job
    assert not widget.send_button.isEnabled() and widget.cancel_button.isEnabled()
    owner.coordinator.lease.allowed = False
    widget.cancel_button.click()
    _complete(qtbot, widget)
    assert active.result()["result"].state == "unknown_outcome"
    assert len(state.requests) == 2 and all(client.is_closed for client in state.clients)


def test_mainwindow_finally_closes_notifications_after_other_panel_failure(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    with AccountRepository(tmp_path / "notification-shutdown.sqlite") as repository:
        coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
        window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        window.show()
        qtbot.waitUntil(lambda: window.ready)
        assert window.notifications.owner is window
        assert window.notifications.thread() is qapp.thread()
        assert QThread.currentThread() is qapp.thread()
        assert not window.notifications.send_button.isEnabled()
        entered, transport_closed = threading.Event(), threading.Event()
        clients, calls = [], []

        async def handler(request):
            calls.append(request)
            entered.set()
            await asyncio.Event().wait()

        class Transport(httpx.MockTransport):
            async def aclose(self):
                transport_closed.set()

        def factory(token, **kwargs):
            client = TelegramClient(token, transport=Transport(handler), **kwargs)
            clients.append(client)
            return client

        async def broken_trade_close():
            raise RuntimeError(PRIVATE)

        # The simulator remains the only composition. The explicit test override
        # exercises the manual panel using an injected offline transport.
        window.simulate = False
        window.notifications._client_factory = factory
        window.notifications.token.setText(TOKEN)
        window.notifications.destination.setText("123")
        window.notifications.message.setPlainText(PRIVATE)
        window.notifications._refresh()
        window.trades.aclose = broken_trade_close
        try:
            window.notifications.send_button.click()
            qtbot.waitUntil(entered.is_set)
            coordinator.lease.allowed = False
            window.close()
            qtbot.waitUntil(lambda: window._closed, timeout=5000)
            assert not window.bridge._thread.is_alive()
            assert window.bridge.shutdown_error == "RuntimeError"
            assert transport_closed.is_set() and all(client.is_closed for client in clients)
            assert len(calls) == 1
            assert PRIVATE not in window.log.toPlainText()
        finally:
            if not window._closed:
                window.close()
                qtbot.waitUntil(lambda: window._closed, timeout=5000)
            window.deleteLater()


def _write_envelope(path, version, values, references=()):
    raw = json.dumps(
        {"format": f"fsm-panel-settings-v{version}", "values": values, "secrets": list(references)}
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


@pytest.mark.parametrize(
    "key", ["PROXY_FOR_TG", "proxy_for_tg", "tg_bot_chat_id", "tradeoffer_link"]
)
def test_v1_newly_sensitive_fields_load_read_only_and_both_next_save_files_are_sealed(
    tmp_path, key
):
    target = tmp_path / "settings.vault.json"
    original = _write_envelope(target, 1, {key: PROXY, "revision": "old"})
    vault = SettingsVault(MemorySecretStore())
    document = vault.load(target)
    assert document.get(key) == PROXY
    assert document.to_dict()[key] == "[redacted]"
    assert target.read_bytes() == original
    backup = target.with_suffix(target.suffix + ".bak")
    assert not backup.exists()
    document.set("revision", "new")
    vault.save(document, target)
    for path, revision in ((target, "new"), (backup, "old")):
        raw = path.read_bytes()
        assert json.loads(raw)["format"] == "fsm-panel-settings-v2"
        assert PROXY.encode() not in raw and b"synthetic_password" not in raw
        restored = vault.load(path)
        assert restored.get(key) == PROXY and restored.get("revision") == revision


def test_v1_nested_proxy_migrates_while_historical_references_stay_required(tmp_path):
    target = tmp_path / "settings.vault.json"
    secrets = MemorySecretStore()
    secrets.put("settings-synthetic-bot", json.dumps(TOKEN).encode())
    secrets.put("settings-synthetic-password", json.dumps(PRIVATE).encode())
    original = _write_envelope(
        target,
        1,
        {"TG_BOT_API_KEY": None, "extensions": [{"proxy_for_tg": PROXY, "PASSWORD": None}]},
        [
            {"path": ["TG_BOT_API_KEY"], "reference": "settings-synthetic-bot"},
            {"path": ["extensions", 0, "PASSWORD"], "reference": "settings-synthetic-password"},
        ],
    )
    vault = SettingsVault(secrets)
    document = vault.load(target)
    assert document.get("TG_BOT_API_KEY") == TOKEN
    assert document.get("extensions")[0] == {"proxy_for_tg": PROXY, "PASSWORD": PRIVATE}
    assert target.read_bytes() == original
    assert not any(secret in repr(document) for secret in (TOKEN, PROXY, PRIVATE))
    vault.save(document, target)
    for path in (target, target.with_suffix(target.suffix + ".bak")):
        assert not any(secret.encode() in path.read_bytes() for secret in (TOKEN, PROXY, PRIVATE))
        assert vault.load(path).get("extensions")[0]["proxy_for_tg"] == PROXY


@pytest.mark.parametrize(
    "key", ["PROXY_FOR_TG", "proxy_for_tg", "tg_bot_chat_id", "tradeoffer_link"]
)
def test_v2_unsealed_new_secret_is_rejected_without_mutating_file(tmp_path, key):
    target = tmp_path / "settings.vault.json"
    original = _write_envelope(target, 2, {"extensions": [{key: PROXY}]})
    with pytest.raises(ValidationError, match="^settings_vault_load_failed$") as caught:
        SettingsVault(MemorySecretStore()).load(target)
    assert target.read_bytes() == original
    assert PROXY not in str(caught.value)


@pytest.mark.parametrize(
    "values",
    [
        {"TG_BOT_CHAT_ID": "123"},
        {"extensions": [{"api_key": TOKEN}]},
        {"PROXY_FOR_TG": {"PASSWORD": PRIVATE}},
        {"tradeoffer_link": [{"credentials": PRIVATE}]},
    ],
)
def test_v1_migration_does_not_bypass_preexisting_nested_secret_requirements(tmp_path, values):
    target = tmp_path / "settings.vault.json"
    original = _write_envelope(target, 1, values)
    with pytest.raises(ValidationError, match="^settings_vault_load_failed$"):
        SettingsVault(MemorySecretStore()).load(target)
    assert target.read_bytes() == original


def test_mainwindow_can_open_previous_vault_without_rewriting_it(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    target = tmp_path / "settings.vault.json"
    original = _write_envelope(target, 1, {"PROXY_FOR_TG": PROXY})
    with AccountRepository(tmp_path / "v1-startup.sqlite") as repository:
        coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
        window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        try:
            window.show()
            qtbot.waitUntil(lambda: window.ready)
            window.notifications.load_button.click()
            assert window.notifications.proxy.text() == PROXY
            assert not window.notifications.send_button.isEnabled()
            assert target.read_bytes() == original
        finally:
            window.close()
            qtbot.waitUntil(lambda: window._closed, timeout=5000)
            window.deleteLater()
