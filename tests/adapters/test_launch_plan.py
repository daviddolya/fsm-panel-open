import builtins
import os
import random
import subprocess
import time
from dataclasses import FrozenInstanceError, replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fsm_panel.adapters.launch_plan import (
    DETACHED_PROCESS,
    STEAM_ID_BASE,
    CreateDirectorySymlink,
    LaunchFacts,
    LaunchPlanError,
    LaunchSettings,
    MakeDirectories,
    RemoveFile,
    build_launch_plan,
    prepare_loginusers,
    userdata_id,
)


@pytest.fixture
def settings():
    return LaunchSettings(
        steam_path=r"C:\SyntheticSteam\steam.exe",
        steam_launch_options="",
        additional_launch_options="",
        cs_width=640,
        cs_height=480,
        no_avast_mode=False,
        steam_launch_only=False,
        ancient_map_preload=False,
        running_2vs2=False,
        user_profile_new_link=r"C:\SyntheticProfiles",
        enable_shaders_caching=True,
    )


@pytest.fixture
def facts():
    return LaunchFacts(False, False, False)


def plan(settings, facts, **kwargs):
    inputs = {
        "login": "synthetic_user",
        "update_mode": False,
        "environment": {},
        "facts": facts,
    }
    inputs.update(kwargs)
    return build_launch_plan(settings, **inputs)


def test_exact_default_game_string_and_empty_argv_fields(settings, facts):
    result = plan(settings, facts)
    assert result.main == (
        " -applaunch 730 -language english +exec autoexec.cfg -w 640 -h 480"
        " -console -condebug -conclearlog -con_logfile synthetic_user.log  -novid -nojoy"
    )
    assert result.argv == (
        r"C:\SyntheticSteam\steam.exe",
        "",
        "-applaunch",
        "730",
        "-language",
        "english",
        "+exec",
        "autoexec.cfg",
        "-w",
        "640",
        "-h",
        "480",
        "-console",
        "-condebug",
        "-conclearlog",
        "-con_logfile",
        "synthetic_user.log",
        "",
        "-novid",
        "-nojoy",
    )
    assert result.status == "Starting"
    assert result.used_port == 21201
    assert result.creationflags == DETACHED_PROCESS == 8
    assert result.shell is False
    assert result.environment is None
    assert result.profile_directory is None
    assert result.shader_cache is None


@pytest.mark.parametrize("no_avast", [False, True])
@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("steam_only", [False, True])
def test_independent_ipc_login_allowmultiple_and_env_branches(
    settings, facts, no_avast, update, steam_only
):
    settings = replace(
        settings,
        steam_launch_options="-silent ",
        no_avast_mode=no_avast,
        steam_launch_only=steam_only,
    )
    result = plan(settings, facts, update_mode=update, ipc_random=9999)
    assert result.main.startswith("-login -silent" if update else "-silent")
    assert ("-master_ipc_name_override synthetic_user9999" in result.main) is (no_avast or update)
    assert ("-allowmultiple" in result.main) is (no_avast and not steam_only)
    assert ("-applaunch 730" in result.main) is not steam_only
    assert ("-novid" in result.main) is not steam_only
    assert (result.environment is not None) is no_avast
    assert (result.profile_directory is not None) is no_avast


@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("preload", [False, True])
@pytest.mark.parametrize("duo", [False, True])
@pytest.mark.parametrize("steam_only", [False, True])
def test_preload_requires_update_preload_and_game_branch(
    settings, facts, update, preload, duo, steam_only
):
    settings = replace(
        settings,
        ancient_map_preload=preload,
        running_2vs2=duo,
        steam_launch_only=steam_only,
    )
    result = plan(settings, facts, update_mode=update, ipc_random=1)
    expected = update and preload and not steam_only
    assert ("+map de_inferno" in result.main) is (expected and duo)
    assert ("+map de_ancient" in result.main) is (expected and not duo)


def test_rstrip_is_unicode_aware_but_leading_and_interior_whitespace_survive(settings, facts):
    settings = replace(settings, steam_launch_only=True, steam_launch_options='  "a b"\tc\n\u2003')
    result = plan(settings, facts)
    assert result.main == '  "a b"\tc'
    assert result.argv[1:] == ("", "", '"a', 'b"\tc')


def test_steam_only_global_word_removal_retains_hyphens_ids_and_banned_options(settings, facts):
    settings = replace(
        settings,
        steam_launch_only=True,
        steam_launch_options="-applaunch 730 xapplaunchy  applaunch -d3d9ex",
        additional_launch_options="unreachable",
    )
    result = plan(settings, replace(facts, launch_dota2_exists=True))
    assert result.main == "- 730 xy   -d3d9ex"
    assert result.argv[1:] == ("-", "730", "xy", "", "", "-d3d9ex")


def test_empty_steam_only_command_keeps_one_empty_argument(settings, facts):
    result = plan(replace(settings, steam_launch_only=True), facts)
    assert result.main == ""
    assert result.argv == (settings.steam_path, "")


def test_dota_replacement_is_global_but_precedes_logfile_and_additional_options(settings, facts):
    settings = replace(
        settings,
        steam_launch_options="prefix730suffix",
        cs_width="1730",
        cs_height=730,
        no_avast_mode=True,
        additional_launch_options="-custom 730",
    )
    result = plan(
        settings, replace(facts, launch_dota2_exists=True), login="user730", ipc_random=730
    )
    assert result.main.startswith("prefix570suffix -master_ipc_name_override user570570 ")
    assert "-applaunch 570" in result.main
    assert "-w 1570 -h 570" in result.main
    assert "-con_logfile user730.log -custom 730" in result.main


def test_new_params_use_substring_membership_even_inside_other_values(settings, facts):
    settings = replace(
        settings,
        ancient_map_preload=True,
        additional_launch_options='"prefix-novid-suffix" -nojoystick "+map de_ancient_extra"',
    )
    result = plan(settings, facts, update_mode=True, ipc_random=1)
    assert result.main.endswith(settings.additional_launch_options)
    assert "-novid" not in result.argv
    assert "-nojoy" not in result.argv
    assert "de_ancient" not in result.argv


def test_banned_parameters_are_global_space_prefixed_substrings(settings, facts):
    settings = replace(
        settings,
        steam_launch_options="-d3d9ex -d3d9ex_extra\t-d3d9ex",
        additional_launch_options=(
            "-disable_d3d9_hacks -disable_d3d9_hacksTAIL -dxlevel 900"
            " -dxlevel 90 -d3d9ex -novid -nojoy"
        ),
    )
    result = plan(settings, facts)
    assert result.main.startswith("-d3d9ex_extra\t-d3d9ex ")
    assert result.main.endswith("synthetic_user.logTAIL0 -novid -nojoy")


@pytest.mark.parametrize("override,expected", [(None, r"C:\SyntheticSteam\steam.exe"), ("", "")])
def test_only_none_uses_default_executable(settings, facts, override, expected):
    assert plan(settings, facts, steam_path=override).argv[0] == expected


def test_executable_path_is_one_argv_field_without_quoting_or_trimming(settings, facts):
    assert plan(settings, facts, steam_path='  C:\\Steam Folder\\steam.exe "').argv[0] == (
        '  C:\\Steam Folder\\steam.exe "'
    )


@pytest.mark.parametrize("random_value", [1, 9999])
def test_ipc_random_inclusive_endpoints(settings, facts, random_value):
    result = plan(replace(settings, no_avast_mode=True), facts, ipc_random=random_value)
    assert f"synthetic_user{random_value}" in result.argv


@pytest.mark.parametrize("random_value", [None, 0, -1, 10000, True, "1", 1.0])
def test_ipc_random_validation_is_content_free(settings, facts, random_value):
    with pytest.raises(LaunchPlanError, match="^launch_ipc_random_range$"):
        plan(settings, facts, update_mode=True, ipc_random=random_value)
    # This input is not consumed in the branch that never calls randint.
    assert "master_ipc" not in plan(settings, facts, ipc_random=random_value).main


def test_inherited_environment_is_not_replaced_even_in_update_mode(settings, facts):
    inherited = {"VPROJECT": "legacy", "PRIVATE_TOKEN": "synthetic_secret"}
    result = plan(settings, facts, update_mode=True, ipc_random=7, environment=inherited)
    assert result.environment is None
    assert inherited == {"VPROJECT": "legacy", "PRIVATE_TOKEN": "synthetic_secret"}


def test_profile_env_is_an_immutable_copy_and_paths_are_literal(settings, facts):
    settings = replace(settings, no_avast_mode=True, user_profile_new_link="C:/profiles\\")
    inherited = {"VPROJECT": "legacy", "USERPROFILE": r"C:\Host", "PRIVATE_TOKEN": "secret"}
    result = plan(settings, facts, login="../synthetic", ipc_random=7, environment=inherited)
    expected_profile = "C:/profiles\\\\../synthetic"
    assert result.environment == {
        "VPROJECT": "1",
        "USERPROFILE": expected_profile,
        "LOCALAPPDATA": expected_profile + r"\AppData\Local",
        "PRIVATE_TOKEN": "secret",
    }
    assert result.profile_directory == MakeDirectories(expected_profile + r"\AppData\Local")
    assert result.profile_directory_on_exception == "propagate"
    inherited["PRIVATE_TOKEN"] = "changed"
    assert result.environment["PRIVATE_TOKEN"] == "secret"
    with pytest.raises(TypeError):
        result.environment["PRIVATE_TOKEN"] = "changed"
    with pytest.raises(FrozenInstanceError):
        result.status = "changed"


@pytest.mark.parametrize("no_avast", [False, True])
@pytest.mark.parametrize("caching", [False, True])
def test_shader_flag_is_inverted_and_runs_only_for_no_avast(settings, facts, no_avast, caching):
    result = plan(
        replace(settings, no_avast_mode=no_avast, enable_shaders_caching=caching),
        facts,
        ipc_random=1,
        environment={"USERPROFILE": r"C:\Host"},
    )
    assert (result.shader_cache is not None) is (no_avast and not caching)


@pytest.mark.parametrize(
    "exists,is_link", [(False, False), (True, False), (False, True), (True, True)]
)
def test_shader_operations_use_exists_or_islink_and_nonrecursive_remove(
    settings, facts, exists, is_link
):
    result = plan(
        replace(settings, no_avast_mode=True, enable_shaders_caching=False),
        replace(facts, shader_link_exists=exists, shader_link_is_symlink=is_link),
        ipc_random=1,
        environment={"USERPROFILE": r"D:\OriginalHost"},
    )
    group = result.shader_cache
    assert group is not None
    local = r"C:\SyntheticProfiles\synthetic_user\AppData\Local"
    assert group.operations[0] == MakeDirectories(local)
    assert group.operations[-1] == CreateDirectorySymlink(
        r"D:\OriginalHost\AppData\Local\NVIDIA", local + r"\NVIDIA"
    )
    assert group.operations[1:-1] == (
        (RemoveFile(local + r"\NVIDIA"),) if exists or is_link else ()
    )
    assert group.on_exception == "report_and_continue"
    assert group.caught_error is None


def test_missing_host_userprofile_is_a_caught_shader_error_not_an_empty_target(settings, facts):
    result = plan(
        replace(settings, no_avast_mode=True, enable_shaders_caching=False), facts, ipc_random=1
    )
    assert result.profile_directory is not None
    assert result.shader_cache is not None
    assert result.shader_cache.operations == ()
    assert result.shader_cache.caught_error == "missing_userprofile"
    assert result.environment["USERPROFILE"] == r"C:\SyntheticProfiles\synthetic_user"
    assert result.environment["VPROJECT"] == "1"


def test_empty_host_userprofile_is_valid_strict_lookup(settings, facts):
    result = plan(
        replace(settings, no_avast_mode=True, enable_shaders_caching=False),
        facts,
        ipc_random=1,
        environment={"USERPROFILE": ""},
    )
    assert result.shader_cache.caught_error is None
    assert result.shader_cache.operations[-1].target == r"\AppData\Local\NVIDIA"


def test_plans_and_settings_repr_do_not_disclose_inputs_or_inherited_environment(settings, facts):
    marker = "synthetic_private_marker"
    settings = replace(
        settings,
        steam_path=marker,
        steam_launch_options=marker,
        additional_launch_options=marker,
        user_profile_new_link=marker,
        no_avast_mode=True,
        enable_shaders_caching=False,
    )
    result = plan(settings, facts, login=marker, ipc_random=1, environment={marker: marker})
    assert marker not in repr(settings)
    assert marker not in repr(result)
    assert marker not in repr(result.profile_directory)
    linked = plan(settings, facts, ipc_random=1, environment={"USERPROFILE": marker})
    assert marker not in repr(linked.shader_cache)
    assert result.environment[marker] == marker
    assert marker in result.main


@pytest.mark.parametrize(
    "value", [0, -1, STEAM_ID_BASE, STEAM_ID_BASE - 1, STEAM_ID_BASE + 2**32, 2**200]
)
def test_userdata_subtraction_has_no_unsigned_mask_range_or_nonnegative_check(value):
    assert userdata_id(value) == value - 76561197960265728


@given(st.integers())
def test_userdata_preserves_arbitrary_integer_difference(value):
    assert userdata_id(value) + STEAM_ID_BASE == value


@pytest.mark.parametrize("value", ["76561197960265729", True, 1.0, None])
def test_userdata_boundary_does_not_coerce_types(value):
    with pytest.raises(LaunchPlanError, match="^launch_integer_required$"):
        userdata_id(value)


def test_userdata_rebuild_boundary_rejects_custom_numeric_dispatch():
    class CustomInt(int):
        def __sub__(self, other):
            pytest.fail("custom subtraction must not execute")

    with pytest.raises(LaunchPlanError, match="^launch_integer_required$"):
        userdata_id(CustomInt(4))


def test_loginusers_exact_text_metadata_and_legacy_registry_command():
    result = prepare_loginusers(
        r"C:\SyntheticSteam\steam.exe", steam_id=42, login="example", now=1000.5
    )
    expected = (
        '\n                    "users"\n'
        "                    {\n"
        '                        "42"\n'
        "                        {\n"
        '                            "AccountName"        "example"\n'
        '                            "PersonaName"        ""\n'
        '                            "RememberPassword"        "1"\n'
        '                            "WantsOfflineMode"        "0"\n'
        '                            "SkipOfflineModeWarning"        "0"\n'
        '                            "AllowAutoLogin"        "1"\n'
        '                            "MostRecent"        "1"\n'
        '                            "Timestamp"        "820"\n'
        "                        }\n"
        "                    }\n"
        "                    "
    )
    assert result.content == expected
    assert result.path == r"C:\SyntheticSteam\config\loginusers.vdf"
    assert result.registry_command == (
        'reg add "HKCU\\Software\\Valve\\Steam" /v AutoLoginUser /t REG_SZ /d example /f'
    )
    assert result.timestamp == 820
    assert result.encoding == "utf-8"
    assert result.file_mode == "w"
    assert result.registry_shell is True
    assert result.registry_capture_stdout is True
    assert result.registry_capture_stderr is True
    assert result.registry_text is True
    assert result.registry_check is False
    assert result.on_exception == "report_and_continue"


@pytest.mark.parametrize(
    "now,expected", [(1000.5, 820), (1001.5, 822), (179.5, 0), (178.5, -2), (0, -180)]
)
def test_loginusers_subtracts_180_before_rounding_with_python_ties_to_even(now, expected):
    result = prepare_loginusers("steam.exe", steam_id=-1, login="synthetic", now=now)
    assert result.timestamp == expected
    assert f'"Timestamp"        "{expected}"' in result.content
    assert '"-1"' in result.content


@pytest.mark.parametrize(
    "steam_path,expected",
    [
        ("steam.exe", r"\config\loginusers.vdf"),
        ("C:/Steam/steam.exe", r"\config\loginusers.vdf"),
        ("", r"\config\loginusers.vdf"),
        ("C:\\Steam\\", r"C:\Steam\config\loginusers.vdf"),
        (r"relative\steam.exe", r"relative\config\loginusers.vdf"),
    ],
)
def test_loginusers_path_is_literal_backslash_split_slice_join(steam_path, expected):
    assert prepare_loginusers(steam_path, steam_id=42, login="synthetic", now=180).path == expected


def test_loginusers_keeps_unescaped_quotes_newlines_shell_metacharacters_and_percent():
    login = 'synthetic"\n%name & echo value'
    result = prepare_loginusers("steam.exe", steam_id=42, login=login, now=180)
    assert '"AccountName"        "' + login + '"' in result.content
    assert result.registry_command.endswith("/d " + login + " /f")
    assert login not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.content = "changed"


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf"), True, "180", None])
def test_loginusers_rebuild_boundary_rejects_invalid_time(now):
    with pytest.raises(LaunchPlanError, match="^launch_finite_time_required$"):
        prepare_loginusers("steam.exe", steam_id=42, login="synthetic", now=now)


@pytest.mark.parametrize("dimension", [True, 1.5, None])
def test_settings_dimension_validation_is_separate_from_legacy_formatting(settings, dimension):
    with pytest.raises(LaunchPlanError, match="^launch_dimension_type$"):
        replace(settings, cs_width=dimension)


def test_validated_dimensions_keep_negative_and_unparsed_string_values(settings, facts):
    result = plan(replace(settings, cs_width=-2, cs_height="480 x"), facts)
    assert "-w -2 -h 480 x" in result.main


def test_other_input_errors_have_no_user_supplied_content(settings, facts):
    with pytest.raises(LaunchPlanError, match="^launch_boolean_required$"):
        replace(settings, no_avast_mode="synthetic_private_flag")
    with pytest.raises(LaunchPlanError, match="^launch_string_required$"):
        replace(settings, steam_path=None)
    with pytest.raises(LaunchPlanError, match="^launch_boolean_required$"):
        LaunchFacts(1, False, False)
    with pytest.raises(LaunchPlanError, match="^launch_environment_strings_required$"):
        plan(settings, facts, environment={"synthetic_private_key": 4})


def test_all_preparation_is_pure_and_uses_only_injected_facts(settings, facts, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure planner accessed a host dependency")

    with monkeypatch.context() as patch:
        for owner, name in (
            (builtins, "open"),
            (os, "makedirs"),
            (os, "remove"),
            (os, "symlink"),
            (os.path, "exists"),
            (os.path, "islink"),
            (subprocess, "Popen"),
            (subprocess, "run"),
            (random, "randint"),
            (time, "time"),
        ):
            patch.setattr(owner, name, forbidden)
        patch.setattr(os, "environ", None)
        result = plan(
            replace(settings, no_avast_mode=True, enable_shaders_caching=False),
            replace(facts, launch_dota2_exists=True, shader_link_exists=True),
            ipc_random=23,
            environment={"USERPROFILE": r"C:\Injected"},
        )
        loginusers = prepare_loginusers(
            settings.steam_path, steam_id=42, login="synthetic", now=180
        )
        assert result.argv[0] == settings.steam_path
        assert "-applaunch 570" in result.main
        assert loginusers.timestamp == 0
