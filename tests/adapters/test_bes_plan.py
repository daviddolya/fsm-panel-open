import pytest

from fsm_panel.adapters.bes import limit_command
from fsm_panel.adapters.bes_plan import build_legacy_bes_plan
from fsm_panel.core.models import ValidationError


def test_real_targets_and_debug_output_are_different():
    plan = build_legacy_bes_plan(
        cwd=r"C:\Panel Folder",
        csgo_path=r"D:\Game",
        steam_path=r"E:\Steam\Steam.exe",
        reduction_percent=25,
    )
    assert plan.command == (
        r'C:\Panel Folder\BES\BES.exe -J "D:\Game\game\bin\win64\cs2.exe" 25 '
        r'"E:\Steam\bin\cef\cef.win64\steamwebhelper.exe" 25'
    )
    assert plan.debug_arguments == (
        "-J",
        r'"D:\Game\cs2.exe"',
        "50",
        r'"E:\Steam\GameOverlayUI.exe"',
        "50",
    )
    assert plan.daemon_thread and plan.subprocess_method == "call"
    assert not plan.shell and not plan.exit_code_used
    assert "Panel Folder" not in repr(plan)


@pytest.mark.parametrize("value", [-20, 0, 1, 25, 99, 100, 1000])
def test_integer_is_neither_clamped_nor_inverted(value):
    plan = build_legacy_bes_plan(
        cwd="", csgo_path="", steam_path="forward/slashes/Steam.exe", reduction_percent=value
    )
    assert plan.command == (
        rf'\BES\BES.exe -J "\game\bin\win64\cs2.exe" {value} '
        rf'"\bin\cef\cef.win64\steamwebhelper.exe" {value}'
    )
    if not 1 <= value <= 99:
        with pytest.raises(ValidationError):
            limit_command(123, value)


@pytest.mark.parametrize("value", [True, "25", 25.0, None])
def test_new_typed_boundary_rejects_non_integer(value):
    with pytest.raises(ValidationError, match="bes_plan_integer_required"):
        build_legacy_bes_plan(cwd="", csgo_path="", steam_path="", reduction_percent=value)
