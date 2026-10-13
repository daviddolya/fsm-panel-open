import pytest

from fsm_panel.adapters.windows import FakeWindowLookup, WindowInfo, WindowsWindowLookup
from fsm_panel.core import UnavailableError


def test_window_lookup_filters_only_requested_pid_and_hides_title_in_repr():
    windows = [WindowInfo(1, 10, "synthetic_private_title"), WindowInfo(2, 20, "other")]
    lookup = FakeWindowLookup(windows)
    assert lookup.for_pid(10) == [windows[0]]
    assert lookup.for_pid(30) == []
    assert "synthetic_private_title" not in repr(windows[0])


def test_windows_lookup_non_windows_is_unavailable(monkeypatch):
    monkeypatch.setattr("fsm_panel.adapters.windows.os.name", "posix")
    with pytest.raises(UnavailableError):
        WindowsWindowLookup().for_pid(1)
