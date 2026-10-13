import threading
import time
from types import SimpleNamespace

import pytest

from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.video import VideoPanel


@pytest.fixture
def panel(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    owner = SimpleNamespace(bridge=AsyncBridge(), _closing=False)
    widget = VideoPanel(owner)
    qtbot.addWidget(widget)
    widget.show()
    template = tmp_path / "panel.txt"
    steam = tmp_path / "steam.txt"
    template.write_text('"VendorID" "1"\n"DeviceID" "2"\n"unrelated" "1"\n', encoding="utf-8")
    steam.write_text('"VendorID" "8"\n"DeviceID" "9"\n', encoding="utf-8")
    widget.template_path.setText(str(template))
    widget.steam_path.setText(str(steam))
    yield widget, template, steam
    owner._closing = True
    owner.bridge.request_shutdown(widget.aclose())
    qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=5000)


def wait_ready(qtbot, widget):
    qtbot.waitUntil(lambda: widget._future is None, timeout=5000)


def test_preview_then_apply_changes_only_explicit_template_and_preserves_backup(qtbot, panel):
    widget, template, steam = panel
    before, source = template.read_bytes(), steam.read_bytes()
    assert not widget.apply_button.isEnabled()
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    assert template.read_bytes() == before
    assert widget.apply_button.isEnabled()
    assert '"unrelated" "1"' in widget.result.toPlainText()
    widget.apply_button.click()
    wait_ready(qtbot, widget)
    assert '"VendorID" "8"' in template.read_text()
    assert '"unrelated" "1"' in template.read_text()
    assert steam.read_bytes() == source
    assert "Резервная копия" in widget.status.text()
    assert not widget.apply_button.isEnabled()


def test_changing_policy_invalidates_preview_and_legacy_result_is_visible(qtbot, panel):
    widget, _, _ = panel
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    widget.policy.setCurrentIndex(1)
    assert not widget.apply_button.isEnabled()
    assert "во всём" in widget.policy_note.text()
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    assert '"unrelated" "8"' in widget.result.toPlainText()


def test_external_source_change_rejects_apply_without_success_message(qtbot, panel):
    widget, template, steam = panel
    before = template.read_bytes()
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    steam.write_text('"VendorID" "7"\n"DeviceID" "6"\n')
    widget.apply_button.click()
    wait_ready(qtbot, widget)
    assert "video_preview_stale" in widget.status.text()
    assert template.read_bytes() == before
    assert not widget.apply_button.isEnabled()


def test_missing_files_disable_apply_and_surface_failure(qtbot, panel):
    widget, _, steam = panel
    steam.unlink()
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    assert "не выполнена" in widget.status.text()
    assert not widget.apply_button.isEnabled()


def test_shutdown_waits_for_file_operation_without_blocking_qt(qtbot, panel, monkeypatch):
    import fsm_panel.ui.video as module

    widget, _, _ = panel
    original = module.preview_video_template
    entered, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test_timeout")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "preview_video_template", delayed)
    widget.preview_button.click()
    qtbot.waitUntil(entered.is_set)
    try:
        started = time.monotonic()
        future = widget.owner.bridge.submit(widget.aclose())
        assert time.monotonic() - started < 0.2
        assert not future.done()
    finally:
        release.set()
    qtbot.waitUntil(future.done)
    assert future.exception() is None
    wait_ready(qtbot, widget)
