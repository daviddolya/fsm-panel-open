"""Independent checks of the bounded native loader specification and proposal CLI."""

import copy
import json
import sys

import pytest

from fsm_panel import launch_preview
from fsm_panel.adapters.launch_plan import LaunchPlanError
from fsm_panel.adapters.launch_settings import derive_launch_settings

KEYS = (
    "CS_RESOLUTION",
    "STEAM_LAUNCH_ONLY_MODE",
    "ENABLE_SHADERS_CACHING",
    "BES_REDUCTION_PERCENT",
)
DEFAULTS = ("360x270", False, False, 25)
PRIVATE = "QA_PRIVATE_LEGACY_VALUE_SENTINEL"


@pytest.mark.parametrize("present_mask", range(16))
def test_each_default_is_proposed_only_for_an_absent_key(present_mask):
    # Explicit false values must not be conflated with absence, including BES 0.
    present_values = ("-80x0", [], {}, 0)
    supplied = {
        key: present_values[index] for index, key in enumerate(KEYS) if present_mask & (1 << index)
    }
    before = copy.deepcopy(supplied)
    result = derive_launch_settings(supplied)
    assert supplied == before
    assert result.config_updates == {
        key: DEFAULTS[index] for index, key in enumerate(KEYS) if not present_mask & (1 << index)
    }
    assert result.resolution_source == ("-80x0" if present_mask & 1 else "360x270")
    assert (result.width, result.height) == ((-80, 0) if present_mask & 1 else (360, 270))
    assert result.resolution_fallback is False
    assert result.steam_launch_only is False
    assert result.enable_shaders_caching is False
    assert result.bes_reduction_value == (0 if present_mask & 8 else 25)


@pytest.mark.parametrize(
    "resolution",
    ["+480xinvalid", "invalidx+720", "480x1e3", "480x720.0", "480x0x20", True, {}],
)
def test_any_dimension_failure_resets_both_dimensions_without_replacing_the_source(resolution):
    source = {key: False for key in KEYS}
    source["CS_RESOLUTION"] = resolution
    before = copy.deepcopy(source)
    result = derive_launch_settings(source)
    assert (result.width, result.height) == (360, 270)
    assert result.resolution_fallback is True
    assert result.resolution_source == resolution
    assert result.config_updates == {}
    assert source == before


@pytest.mark.parametrize("oversized_width", [False, True])
def test_python_integer_digit_bound_falls_back_for_both_dimensions(oversized_width):
    original_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        excessive = "8" * 641
        resolution = excessive + "x720" if oversized_width else "480x" + excessive
        result = derive_launch_settings({"CS_RESOLUTION": resolution})
        assert result.resolution_source == resolution
        assert result.resolution_fallback is True
        assert (result.width, result.height) == (360, 270)
        assert "CS_RESOLUTION" not in result.config_updates
    finally:
        sys.set_int_max_str_digits(original_limit)


def test_nested_present_values_are_detached_from_the_callers_settings():
    source = {
        "CS_RESOLUTION": {"nested": [PRIVATE]},
        "STEAM_LAUNCH_ONLY_MODE": [False, {"secret": PRIVATE}],
        "ENABLE_SHADERS_CACHING": {"flag": [False]},
        "BES_REDUCTION_PERCENT": [PRIVATE],
    }
    expected = copy.deepcopy(source)
    result = derive_launch_settings(source)
    source["STEAM_LAUNCH_ONLY_MODE"][1]["secret"] = "changed"
    source["ENABLE_SHADERS_CACHING"]["flag"].clear()
    result.resolution_source["nested"].append("changed only in result")
    result.bes_reduction_value.clear()
    assert result.steam_launch_only_value == expected["STEAM_LAUNCH_ONLY_MODE"]
    assert result.shaders_caching_value == expected["ENABLE_SHADERS_CACHING"]
    assert source["CS_RESOLUTION"] == expected["CS_RESOLUTION"]
    assert source["BES_REDUCTION_PERCENT"] == expected["BES_REDUCTION_PERCENT"]
    assert result.steam_launch_only is True and result.enable_shaders_caching is True
    assert PRIVATE not in repr(result)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), (1, 2), {1: "value"}])
def test_invalid_selected_json_values_are_rejected_before_truthiness(bad):
    with pytest.raises(LaunchPlanError) as caught:
        derive_launch_settings({"BES_REDUCTION_PERCENT": {"nested": [bad]}})
    assert caught.value.code == "launch_settings_json_required"


@pytest.fixture
def proposal(tmp_path):
    return (
        tmp_path / "input.json",
        tmp_path / "proposal.json",
        {
            "settings": {
                "steam_path": r"Z:\synthetic\steam.exe",
                "steam_launch_options": "-silent",
                "additional_launch_options": "",
                "no_avast_mode": True,
                "ancient_map_preload": False,
                "running_2vs2": False,
                "user_profile_new_link": r"Z:\synthetic-profiles",
            },
            "legacy_settings": {},
            "login": "synthetic",
            "update_mode": False,
            "environment": {"USERPROFILE": r"Z:\synthetic-user", "PRIVATE_TOKEN": PRIVATE},
            "facts": {
                "launch_dota2_exists": False,
                "shader_link_exists": False,
                "shader_link_is_symlink": False,
            },
            "ipc_random": 42,
        },
    )


def run_proposal(proposal, capsys):
    source, output, document = proposal
    source.write_text(json.dumps(document), encoding="utf-8")
    before = source.read_bytes()
    status = launch_preview.main(["--input", str(source), "--output", str(output)])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert PRIVATE not in captured.out
    assert source.read_bytes() == before
    return status, json.loads(captured.out)


@pytest.mark.parametrize("steam_only,expected_only", [("false", True), ([], False)])
@pytest.mark.parametrize("shaders,expected_caching", [({"false": 0}, True), (None, False)])
def test_legacy_truthiness_controls_actual_game_and_inverted_shader_proposals(
    proposal, capsys, steam_only, expected_only, shaders, expected_caching
):
    _, output, document = proposal
    document["legacy_settings"] = {
        "CS_RESOLUTION": "-80x0",
        "STEAM_LAUNCH_ONLY_MODE": steam_only,
        "ENABLE_SHADERS_CACHING": shaders,
        "BES_REDUCTION_PERCENT": {"private": PRIVATE},
    }
    status, summary = run_proposal(proposal, capsys)
    assert status == 0 and summary["operation"] == "preview_only"
    payload = json.loads(output.read_text())
    assert payload["executable"] is False
    assert ("-applaunch" not in payload["argv"]) is expected_only
    if not expected_only:
        assert payload["argv"][payload["argv"].index("-w") + 1] == "-80"
        assert payload["argv"][payload["argv"].index("-h") + 1] == "0"
    if expected_caching:
        assert payload["shader_cache"] is None
    else:
        assert [action["kind"] for action in payload["shader_cache"]["operations"]] == [
            "MakeDirectories",
            "CreateDirectorySymlink",
        ]
    assert payload["settings_derivation"] == {
        "resolution_fallback": False,
        "missing_keys_with_recovered_defaults": [],
    }
    assert PRIVATE not in output.read_text()


def test_omitted_legacy_keys_reach_proposal_as_recovered_defaults_without_persistence(
    proposal, capsys
):
    _, output, _ = proposal
    status, _ = run_proposal(proposal, capsys)
    assert status == 0
    payload = json.loads(output.read_text())
    assert payload["argv"][payload["argv"].index("-w") + 1] == "360"
    assert payload["argv"][payload["argv"].index("-h") + 1] == "270"
    assert payload["shader_cache"] is not None
    assert payload["settings_derivation"] == {
        "resolution_fallback": False,
        "missing_keys_with_recovered_defaults": list(KEYS),
    }


@pytest.mark.parametrize(
    "explicit_key", ["cs_width", "cs_height", "steam_launch_only", "enable_shaders_caching"]
)
def test_any_single_explicit_derived_field_is_ambiguous_even_when_false(
    proposal, capsys, explicit_key
):
    _, output, document = proposal
    document["settings"][explicit_key] = False
    status, summary = run_proposal(proposal, capsys)
    assert status == 2 and summary == {"error": "launch_ambiguous_derived_settings"}
    assert not output.exists()


@pytest.mark.parametrize("legacy", [None, [], PRIVATE, {"STEAM_LAUNCH_ONLY_MODE": float("nan")}])
def test_cli_invalid_legacy_object_creates_no_output_or_private_diagnostic(
    proposal, capsys, legacy
):
    _, output, document = proposal
    document["legacy_settings"] = legacy
    status, summary = run_proposal(proposal, capsys)
    assert status == 2 and summary == {"error": "launch_settings_json_required"}
    assert not output.exists()
