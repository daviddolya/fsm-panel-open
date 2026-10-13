"""Pure bounded settings-loader slice recovered at Panel.exe 0x1406b0d40.

Only resolution, Steam-only, shader caching and BES fallback are covered. This
does not reproduce the full initializer or persist its fallback assignments.
"""

import copy
import math
from dataclasses import dataclass, field

from fsm_panel.adapters.launch_plan import LaunchPlanError

_DEFAULTS: dict[str, object] = {
    "CS_RESOLUTION": "360x270",
    "STEAM_LAUNCH_ONLY_MODE": False,
    "ENABLE_SHADERS_CACHING": False,
    "BES_REDUCTION_PERCENT": 25,
}


def _json_value(value: object, depth: int = 0) -> None:
    if depth > 40:
        raise LaunchPlanError("launch_settings_too_deep")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _json_value(child, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_value(child, depth + 1)
        return
    raise LaunchPlanError("launch_settings_json_required")


@dataclass(frozen=True)
class DerivedLaunchSettings:
    resolution_source: object = field(repr=False)
    width: int
    height: int
    resolution_fallback: bool
    steam_launch_only_value: object = field(repr=False)
    steam_launch_only: bool
    shaders_caching_value: object = field(repr=False)
    enable_shaders_caching: bool
    bes_reduction_value: object = field(repr=False)
    config_updates: dict[str, object] = field(repr=False)


def derive_launch_settings(values: dict[str, object]) -> DerivedLaunchSettings:
    """Preserve present JSON values; return missing-key updates without mutation.

    Custom objects and non-finite numbers are rejected at this new API boundary.
    Resolution parsing uses Python split/int, including its configured digit bound.
    The values in the result are detached JSON copies, not settings references.
    """
    if type(values) is not dict:
        raise LaunchPlanError("launch_settings_json_required")
    selected = {key: values.get(key, default) for key, default in _DEFAULTS.items()}
    _json_value(selected)
    selected = copy.deepcopy(selected)
    source = selected["CS_RESOLUTION"]
    fallback = False
    try:
        # A non-string JSON value has no split method in the original.
        if type(source) is not str:
            raise ValueError()
        width_text, height_text = source.split("x")
        width, height = int(width_text), int(height_text)
    except ValueError:
        width, height = 360, 270
        fallback = True
    return DerivedLaunchSettings(
        source,
        width,
        height,
        fallback,
        selected["STEAM_LAUNCH_ONLY_MODE"],
        bool(selected["STEAM_LAUNCH_ONLY_MODE"]),
        selected["ENABLE_SHADERS_CACHING"],
        bool(selected["ENABLE_SHADERS_CACHING"]),
        selected["BES_REDUCTION_PERCENT"],
        {key: copy.deepcopy(default) for key, default in _DEFAULTS.items() if key not in values},
    )
