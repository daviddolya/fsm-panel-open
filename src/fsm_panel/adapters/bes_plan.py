"""Pure legacy BES proposal; deliberately has no process executor.

Panel.exe 0x1409572d0 uses path-wide -J targets, whereas the live BES adapter
requires owned PIDs. These two contracts must not be confused.
"""

from dataclasses import dataclass, field

from fsm_panel.core.models import ValidationError

LEGACY_BES_FALLBACK_PERCENT = 25


@dataclass(frozen=True)
class LegacyBesPlan:
    command: str = field(repr=False)
    debug_arguments: tuple[str, ...] = field(repr=False)
    reduction_percent: int
    daemon_thread: bool = True
    subprocess_method: str = "call"
    shell: bool = False
    exit_code_used: bool = False


def build_legacy_bes_plan(
    *, cwd: str, csgo_path: str, steam_path: str, reduction_percent: int
) -> LegacyBesPlan:
    """Reproduce the observed command for explicitly typed, inert inputs.

    Integers are not clamped or inverted. Rejecting non-integer values is a new
    boundary matching SettingsStore; the original consumer formatted any value.
    This preserves even broken spacing/quoting for characterization, not launch.
    """
    if any(type(value) is not str for value in (cwd, csgo_path, steam_path)):
        raise ValidationError("bes_plan_string_required")
    if type(reduction_percent) is not int:
        raise ValidationError("bes_plan_integer_required")
    steam_dir = "\\".join(steam_path.split("\\")[:-1])
    cs2 = '"' + csgo_path + '\\game\\bin\\win64\\cs2.exe"'
    webhelper = '"' + steam_dir + '\\bin\\cef\\cef.win64\\steamwebhelper.exe"'
    value = format(reduction_percent, "")
    command = cwd + "\\BES\\BES.exe -J " + cs2 + " " + value + " " + webhelper + " " + value
    debug = (
        "-J",
        '"' + csgo_path + '\\cs2.exe"',
        "50",
        '"' + steam_dir + '\\GameOverlayUI.exe"',
        "50",
    )
    return LegacyBesPlan(command, debug, reduction_percent)
