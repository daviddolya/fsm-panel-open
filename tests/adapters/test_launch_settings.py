import pytest

from fsm_panel.adapters.launch_plan import LaunchPlanError
from fsm_panel.adapters.launch_settings import derive_launch_settings


def test_missing_and_invalid_resolution_preserve_different_source_values():
    original: dict[str, object] = {}
    missing = derive_launch_settings(original)
    assert original == {}
    assert missing.resolution_source == "360x270"
    assert (missing.width, missing.height) == (360, 270)
    assert not missing.resolution_fallback
    assert missing.config_updates == {
        "CS_RESOLUTION": "360x270",
        "STEAM_LAUNCH_ONLY_MODE": False,
        "ENABLE_SHADERS_CACHING": False,
        "BES_REDUCTION_PERCENT": 25,
    }
    invalid = derive_launch_settings({"CS_RESOLUTION": "private-invalid"})
    assert invalid.resolution_source == "private-invalid"
    assert invalid.resolution_fallback
    assert (invalid.width, invalid.height) == (360, 270)
    assert "CS_RESOLUTION" not in invalid.config_updates
    assert "private-invalid" not in repr(invalid)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1920x1080", (1920, 1080)),
        (" +00360 x -270 ", (360, -270)),
        ("0x0", (0, 0)),
        ("１２３x٤٥٦", (123, 456)),
        ("1_000x2_000", (1000, 2000)),
        ("9" * 100 + "x1", (int("9" * 100), 1)),
    ],
)
def test_exact_lowercase_split_and_python_integer_conversion(value, expected):
    derived = derive_launch_settings({"CS_RESOLUTION": value})
    assert (derived.width, derived.height) == expected
    assert not derived.resolution_fallback


@pytest.mark.parametrize("value", ["640X480", "640×480", "1x2x3", "1x", "x2", "", None, 3, []])
def test_parse_failure_does_not_replace_existing_setting(value):
    derived = derive_launch_settings({"CS_RESOLUTION": value})
    assert (derived.width, derived.height) == (360, 270)
    assert derived.resolution_fallback
    assert derived.resolution_source == value
    assert "CS_RESOLUTION" not in derived.config_updates


@pytest.mark.parametrize("value", [True, False, "false", "", 0, 1, [], [0], {}, {"x": 0}, None])
def test_options_keep_raw_value_and_evaluate_truthiness(value):
    document = {
        "STEAM_LAUNCH_ONLY_MODE": value,
        "ENABLE_SHADERS_CACHING": value,
        "BES_REDUCTION_PERCENT": value,
    }
    derived = derive_launch_settings(document)
    assert derived.steam_launch_only_value == value
    assert derived.steam_launch_only is bool(value)
    assert derived.shaders_caching_value == value
    assert derived.enable_shaders_caching is bool(value)
    assert derived.bes_reduction_value == value
    assert derived.config_updates == {"CS_RESOLUTION": "360x270"}
    if isinstance(value, list):
        value.append("changed")
        assert derived.steam_launch_only_value != value


def test_custom_objects_and_cycles_do_not_execute_user_methods():
    class Untrusted:
        def __bool__(self):
            raise AssertionError("must not execute")

    with pytest.raises(LaunchPlanError, match="launch_settings_json_required"):
        derive_launch_settings({"STEAM_LAUNCH_ONLY_MODE": Untrusted()})
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(LaunchPlanError, match="launch_settings_too_deep"):
        derive_launch_settings({"CS_RESOLUTION": cyclic})
